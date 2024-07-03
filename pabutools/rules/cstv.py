"""
An implementation of the algorithms in:
"Participatory Budgeting with Cumulative Votes", by Piotr Skowron, Arkadii Slinko, Stanisaw Szufa,
Nimrod Talmon (2020), https://arxiv.org/pdf/2009.02690
Programmer: Achiya Ben Natan
Date: 2024/05/16.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable, Collection, Iterable
from enum import Enum

import numpy as np

from pabutools.election import (
    Project,
    CumulativeBallot,
    Instance,
    AbstractCumulativeProfile,
)
from pabutools.rules.budgetallocation import BudgetAllocation
from pabutools.tiebreaking import TieBreakingRule, lexico_tie_breaking
from pabutools.utils import Numeric

logger = logging.getLogger(__name__)

###################################################################
#                                                                 #
#                     Main algorithm                              #
#                                                                 #
###################################################################


class CSTV_Combination(Enum):
    EWT = 1
    EWTC = 2
    MT = 3
    MTC = 4


def cstv(
    instance: Instance,
    profile: AbstractCumulativeProfile,
    combination: CSTV_Combination = None,
    select_project_to_fund_func: Callable = None,
    eligible_projects_func: Callable = None,
    no_eligible_project_func: Callable = None,
    exhaustiveness_postprocess_func: Callable = None,
    initial_budget_allocation: Collection[Project] | None = None,
    tie_breaking: TieBreakingRule = lexico_tie_breaking,
    resoluteness: bool = True,
    verbose: bool = False,
) -> BudgetAllocation | list[BudgetAllocation]:
    """
    The CSTV (Cumulative Support Transfer Voting) budgeting algorithm determines project funding
    based on cumulative support from donor ballots.
    This function evaluates a list of projects and donor profiles, selecting projects for funding
    according to the CSTV methodology.
    It employs various procedures for project selection, eligibility determination, and handling of
    scenarios where no eligible projects exist or to ensure inclusive maximality.
    You can read more about the algorithm in sections 4 and 5 in the paper here:
    https://arxiv.org/pdf/2009.02690 in sections 4 and 5.

    Parameters
    ----------
        instance : Instance
            The list of projects.
        profile : AbstractCumulativeProfile
            The list of donor ballots.
        combination: CSTV_Combination
            Shortcut to use pre-defined sets of parameters (all the different procedures).
        select_project_to_fund_func : callable
            The procedure to select a project for funding.
        eligible_projects_func : callable
            The function to determine eligible projects.
        no_eligible_project_func : callable
            The procedure when there are no eligible projects.
        exhaustiveness_postprocess_func : callable
            The post procedure to handle inclusive maximality.
        initial_budget_allocation : Iterable[:py:class:`~pabutools.election.instance.Project`]
            An initial budget allocation, typically empty.
        tie_breaking : TieBreakingRule, optional
            The tie-breaking rule to use, defaults to lexico_tie_breaking.
        resoluteness : bool, optional
            Set to `False` to obtain an irresolute outcome, where all tied budget allocations are
            returned. Defaults to True.
        verbose : bool, optional
            (De)Activate the display of additional information.
            Defaults to `False`.

    Returns
    -------
        BudgetAllocation
            The list of selected projects.
    """

    if combination is not None:
        if combination == CSTV_Combination.EWT:
            select_project_to_fund_func = select_project_ge
            eligible_projects_func = is_eligible_ge
            no_eligible_project_func = elimination_with_transfers
            exhaustiveness_postprocess_func = reverse_eliminations
        elif combination == CSTV_Combination.EWTC:
            select_project_to_fund_func = select_project_gsc
            eligible_projects_func = is_eligible_gsc
            no_eligible_project_func = elimination_with_transfers
            exhaustiveness_postprocess_func = reverse_eliminations
        elif CSTV_Combination.MT:
            select_project_to_fund_func = select_project_ge
            eligible_projects_func = is_eligible_ge
            no_eligible_project_func = minimal_transfer
            exhaustiveness_postprocess_func = acceptance_of_under_supported_projects
        elif CSTV_Combination.MTC:
            select_project_to_fund_func = select_project_gsc
            eligible_projects_func = is_eligible_gsc
            no_eligible_project_func = minimal_transfer
            exhaustiveness_postprocess_func = acceptance_of_under_supported_projects
        else:
            raise ValueError(
                f"Invalid combination {combination}. Please select an element of the "
                f"CSTV_Combination enumeration."
            )
    else:
        if select_project_to_fund_func is None:
            raise ValueError(
                "If no combination is passed, the select_project_to_fund_func "
                "argument needs to be used"
            )
        if eligible_projects_func is None:
            raise ValueError(
                "If no combination is passed, the eligible_projects_func "
                "argument needs to be used"
            )
        if no_eligible_project_func is None:
            raise ValueError(
                "If no combination is passed, the no_eligible_project_func "
                "argument needs to be used"
            )
        if exhaustiveness_postprocess_func is None:
            raise ValueError(
                "If no combination is passed, the exhaustiveness_postprocess_func "
                "argument needs to be used"
            )

    if not resoluteness:
        raise NotImplementedError(
            'The "resoluteness = False" feature is not yet implemented'
        )

    # Check if all donors donate the same amount
    if not len(set([sum(donor.values()) for donor in profile])) == 1:
        raise ValueError(
            "Not all donors donate the same amount. Change the donations and try again."
        )

    if tie_breaking is None:
        tie_breaking = lexico_tie_breaking
    if initial_budget_allocation is None:
        initial_budget_allocation = BudgetAllocation()
    else:
        initial_budget_allocation = BudgetAllocation(initial_budget_allocation)

    # Initialize the set of selected projects and eliminated projects
    selected_projects = initial_budget_allocation
    eliminated_projects = set()

    # The donations to avoid to mutate the profile passed as argument
    donations = [
        {p: ballot[p] * profile.multiplicity(ballot) for p in instance}
        for ballot in profile
    ]

    # Loop until a halting condition is met
    while True:
        # Calculate the total budget
        budget = sum(sum(donor.values()) for donor in donations)
        if verbose:
            print(f"Budget is: {budget}")

        # Halting condition: if there are no more projects to consider
        if not instance:
            # Perform the inclusive maximality postprocedure
            exhaustiveness_postprocess_func(
                selected_projects,
                donations,
                eliminated_projects,
                select_project_to_fund_func,
                budget,
                tie_breaking,
            )
            if verbose:
                print(f"Final selected projects: {selected_projects}")
            return selected_projects

        # Log donations for each project
        if verbose:
            for project in instance:
                donations = sum(ballot[project] for ballot in donations)
                print(
                    f"Donors and total donations for {project}: {donations}. Price: {project.cost}"
                )

        # Determine eligible projects for funding
        eligible_projects = eligible_projects_func(instance, donations)
        if verbose:
            print(
                f"Eligible projects: {eligible_projects}",
            )

        # If no eligible projects, execute the no-eligible-project procedure
        while not eligible_projects:
            flag = no_eligible_project_func(
                instance,
                donations,
                eliminated_projects,
                select_project_to_fund_func,
            )
            if not flag:
                # Perform the inclusive maximality postprocedure
                exhaustiveness_postprocess_func(
                    selected_projects,
                    donations,
                    eliminated_projects,
                    select_project_to_fund_func,
                    budget,
                    tie_breaking,
                )
                if verbose:
                    print(f"Final selected projects: {selected_projects}")
                return selected_projects
            eligible_projects = eligible_projects_func(instance, donations)

        # Choose one project to fund according to the project-to-fund selection procedure
        tied_projects = select_project_to_fund_func(
            eligible_projects, donations, tie_breaking
        )
        if len(tied_projects) > 1:
            p = tie_breaking.untie(instance, profile, tied_projects)
        else:
            p = tied_projects[0]
        excess_support = sum(donor.get(p.name, 0) for donor in donations) - p.cost
        if verbose:
            print(f"Excess support for {p: {excess_support}}")

        # If the project has enough or excess support
        if excess_support >= 0:
            if excess_support > 0.01:
                # Perform the excess redistribution procedure
                gama = p.cost / (excess_support + p.cost)
                excess_redistribution_procedure(donations, p, gama)
            else:
                # Reset donations for the eliminated project
                print(f"Resetting donations for eliminated project: {p}")
                for donor in donations:
                    donor[p] = 0

            # Add the project to the selected set and remove it from further consideration
            selected_projects.append(p)
            instance.remove(p)
            if verbose:
                print(f"Updated selected projects: {selected_projects}")
            budget -= p.cost
            continue


###################################################################
#                                                                 #
#                     Help functions                              #
#                                                                 #
###################################################################


def excess_redistribution_procedure(
    donors: list[dict[Project, Numeric]],
    selected_project: Project,
    gama: Numeric,
) -> None:
    """
    Distributes the excess support of a selected project to the remaining projects.

    Parameters
    ----------
        donors : list[dict[Project, Numeric]]
            The list of donors.
        selected_project : Project
            The project with the maximum excess support.
        gama : Numeric
            The proportion to distribute.

    Returns
    -------
        None
    """
    logger.debug(f"Distributing excess support of selected project: {selected_project}")
    for donor in donors:
        donor_copy = donor.copy()
        to_distribute = donor_copy[selected_project] * (1 - gama)
        donor[selected_project] = to_distribute
        donor_copy[selected_project] = 0
        total = sum(donor_copy.values())
        for key, donation in donor_copy.items():
            if donation != selected_project:
                if total != 0:
                    part = donation / total
                    donor[key] = donation + to_distribute * part
                donor[selected_project] = 0


def is_eligible_ge(
    projects: Iterable[Project], donors: list[dict[Project, Numeric]]
) -> list[Project]:
    """
    Determines the eligible projects based on the General Election (GE) rule.

    Parameters
    ----------
        projects : Iterable[Project]
            The list of projects.
        donors : list[dict[Project, Numeric]]
            The list of donor ballots.

    Returns
    -------
        list[Project]
            The list of eligible projects.

    Examples
    --------
    >>> project_A = Project("Project A", 35)
    >>> project_B = Project("Project B", 30)
    >>> donor1 = CumulativeBallot({project_A: 5, project_B: 30})
    >>> donor2 = CumulativeBallot({project_A: 10, project_B: 0})
    >>> is_eligible_ge([project_A, project_B], [donor1, donor2])
    [Project B]
    """
    return [
        project
        for project in projects
        if (sum(donor.get(project, 0) for donor in donors) - project.cost) >= 0
    ]


def is_eligible_gsc(
    projects: Iterable[Project], donors: list[dict[Project, Numeric]]
) -> list[Project]:
    """
    Determines the eligible projects based on the Greatest Support to Cost (GSC) rule.

    Parameters
    ----------
        projects : Iterable[Project]
            The list of projects.
        donors : list[dict[Project, Numeric]]
            The list of donor ballots.

    Returns
    -------
        list[Project]
            The list of eligible projects.

    Examples
    --------
    >>> project_A = Project("Project A", 35)
    >>> project_B = Project("Project B", 30)
    >>> donor1 = CumulativeBallot({project_A: 5, project_B: 10})
    >>> donor2 = CumulativeBallot({project_A: 30, project_B: 0})
    >>> is_eligible_gsc([project_A, project_B], [donor1, donor2])
    [Project A]
    """
    return [
        project
        for project in projects
        if (sum(donor.get(project, 0) for donor in donors) / project.cost) >= 1
    ]


def select_project_ge(
    projects: Iterable[Project],
    donors: list[dict[Project, Numeric]],
    during_postprocess: bool = False,
) -> list[Project]:
    """
    Selects the project with the maximum excess support using the General Election (GE) rule.

    Parameters
    ----------
        projects : Iterable[Project]
            The list of projects.
        donors : list[dict[Project, Numeric]]
            The list of donor ballots.
        during_postprocess : bool, optional
            Flag indicating if this selection is part of the inclusive maximality postprocedure.

    Returns
    -------
        list[Project]
            The tied selected projects.

    Examples
    --------
    >>> project_A = Project("Project A", 36)
    >>> project_B = Project("Project B", 30)
    >>> donor1 = CumulativeBallot({project_A: 5, project_B: 10})
    >>> donor2 = CumulativeBallot({project_A: 10, project_B: 0})
    >>> select_project_ge(Instance([project_A, project_B]), [donor1, donor2])[0].name
    'Project B'
    """
    excess_support = {
        project: sum(donor.get(project, 0) for donor in donors) - project.cost
        for project in projects
    }
    max_excess_value = max(excess_support.values())
    max_excess_projects = [
        project
        for project, excess in excess_support.items()
        if excess == max_excess_value
    ]

    if during_postprocess:
        logger.debug(
            f"Selected project by GE method in inclusive maximality postprocedure: "
            f"{max_excess_projects}"
        )
    else:
        logger.debug(f"Selected project by GE method: {max_excess_projects}")

    return max_excess_projects


def select_project_gsc(
    projects: Iterable[Project],
    donors: list[dict[Project, Numeric]],
    during_postprocess: bool = False,
) -> list[Project]:
    """
    Selects the project with the maximum excess support using the General Election (GSC) rule.

    Parameters
    ----------
        projects : Instance
            The list of projects.
        donors : list[dict[Project, Numeric]]
            The list of donor ballots.
        during_postprocess : bool, optional
            Flag indicating if this selection is part of the inclusive maximality postprocedure.

    Returns
    -------
        list[Project]
            The tied selected projects.

    Examples
    --------
    >>> project_A = Project("Project A", 36)
    >>> project_B = Project("Project B", 30)
    >>> donor1 = CumulativeBallot({project_A: 5, project_B: 10})
    >>> donor2 = CumulativeBallot({project_A: 10, project_B: 0})
    >>> select_project_gsc(Instance([project_A, project_B]), [donor1, donor2])[0].name
    Project A
    """
    excess_support = {
        project: sum(donor.get(project, 0) for donor in donors) / project.cost
        for project in projects
    }
    max_excess_value = max(excess_support.values())
    max_excess_projects = [
        project
        for project, excess in excess_support.items()
        if excess == max_excess_value
    ]

    if during_postprocess:
        logger.debug(
            f"Selected project by GSC method in inclusive maximality postprocedure: "
            f"{max_excess_projects}"
        )
    else:
        logger.debug(f"Selected project by GSC method: {max_excess_projects}")

    return max_excess_projects


def elimination_with_transfers(
    projects: list[Project],
    donors: list[dict[Project, Numeric]],
    eliminated_projects: set[Project],
    project_to_fund_selection_procedure: Callable,
) -> bool:
    """
    Eliminates the project with the least excess support and redistributes its support to the
    remaining projects.

    Parameters
    ----------
        projects : list[Project]
            The list of projects.
        donors : list[dict[Project, Numeric]]
            The list of donor ballots.
        eliminated_projects : set[Project]
            The set of eliminated projects.

    Returns
    -------
        bool
            True if the elimination with transfers was successful, False otherwise.

    Examples
    --------
    >>> project_A = Project("Project A", 30)
    >>> project_B = Project("Project B", 30)
    >>> project_C = Project("Project C", 20)
    >>> donor1 = CumulativeBallot({project_A: 5, project_B: 10, project_C: 5})
    >>> donor2 = CumulativeBallot({project_A: 10, project_B: 0, project_C: 5})
    >>> elimination_with_transfers([project_A, project_B, project_C], [donor1, donor2], [], None)
    True
    >>> print(donor1[project_A])
    10.0
    >>> print(donor1[project_B])
    0
    >>> print(donor2[project_A])
    10.0
    >>> print(donor2[project_B])
    0
    >>> print(donor1[project_C])
    10.0
    >>> print(donor2[project_C])
    5.0
    """

    def distribute_project_support(
        all_donors: list[dict[Project, Numeric]],
        eliminated_project: Project,
    ) -> None:
        """
        Distributes the support of an eliminated project to the remaining projects.
        """
        logger.debug(
            f"Distributing support of eliminated project: {eliminated_project}"
        )
        for donor in all_donors:
            to_distribute = donor[eliminated_project]
            total = sum(donor.values()) - to_distribute
            if total == 0:
                continue
            for key, donation in donor.items():
                if key != eliminated_project:
                    part = donation / total
                    donor[key] = donation + to_distribute * part
            donor[eliminated_project] = 0

    if len(projects) < 2:
        logger.debug("Not enough projects to eliminate.")
        if len(projects) == 1:
            eliminated_projects.add(projects.pop())
        return False
    min_project = min(
        projects, key=lambda p: sum(donor.get(p.name, 0) for donor in donors) - p.cost
    )
    logger.debug(f"Eliminating project with least excess support: {min_project.name}")
    distribute_project_support(donors, min_project)
    projects.remove(min_project)
    eliminated_projects.add(min_project)
    return True


def minimal_transfer(
    projects: Iterable[Project],
    donors: list[dict[Project, Numeric]],
    eliminated_projects: set[Project],
    project_to_fund_selection_procedure: Callable,
    tie_breaking: TieBreakingRule = lexico_tie_breaking,
) -> bool:
    """
    Performs minimal transfer of donations to reach the required support for a selected project.

    Parameters
    ----------
    projects : Iterable[Project]
        The list of projects.
    donors : list[dict[Project, Numeric]]
        The list of donor ballots.
    eliminated_projects : set[Project]
        The list of eliminated projects.
    project_to_fund_selection_procedure : callable
        The procedure to select a project for funding.
    tie_breaking : TieBreakingRule, optional
        The tie-breaking rule to use, defaults to lexico_tie_breaking.

    Returns
    -------
    bool
        True if the minimal transfer was successful, False if the project was added to
        eliminated_projects.

    Examples
    --------
    >>> project_A = Project("Project A", 40)
    >>> project_B = Project("Project B", 30)
    >>> donor1 = CumulativeBallot({project_A: 5, project_B: 10})
    >>> donor2 = CumulativeBallot({project_A: 30, project_B: 0})
    >>> minimal_transfer(project_A, project_B], [donor1, donor2], [], select_project_ge)
    True
    >>> print(donor1[project_A])
    9.999999999999996
    >>> print(donor1[project_B])
    5.000000000000034
    >>> print(donor2[project_A])
    30
    >>> print(donor2[project_B])
    0
    """
    projects_with_chance = []
    for project in projects:
        donors_of_selected_project = [
            donor.values()
            for _, donor in enumerate(donors)
            if donor.get(project, 0) > 0
        ]
        sum_of_don = 0
        for d in donors_of_selected_project:
            sum_of_don += sum(d)
        if sum_of_don >= project.cost:
            projects_with_chance.append(project)
    if not projects_with_chance:
        return False
    chosen_project = project_to_fund_selection_procedure(projects_with_chance, donors)[
        0
    ]  # TODO: there should be a tie-breaking here
    donors_of_selected_project = [
        i for i, donor in enumerate(donors) if donor.get(chosen_project.name, 0) > 0
    ]
    logger.debug(f"Selected project for minimal transfer: {chosen_project.name}")

    project_cost = chosen_project.cost

    # Calculate initial support ratio
    total_support = sum(donor.get(chosen_project, 0) for donor in donors)
    r = total_support / project_cost

    # Loop until the required support is achieved
    while r < 1:
        # Check if all donors have their entire donation on the chosen project
        all_on_chosen_project = all(
            sum(donors[i].values()) == donors[i].get(chosen_project, 0)
            for i in donors_of_selected_project
        )

        if all_on_chosen_project:
            for project in projects:
                eliminated_projects.add(copy.deepcopy(project))
            return False

        for i in donors_of_selected_project:
            donor = donors[i]
            total = sum(donor.values()) - donor.get(chosen_project, 0)
            donation = donor.get(chosen_project, 0)
            if total > 0:
                to_distribute = min(total, donation / r - donation)
                for proj_name, proj_donation in donor.items():
                    if proj_name != chosen_project and proj_donation > 0:
                        change = to_distribute * proj_donation / total
                        donor[proj_name] -= change
                        donor[chosen_project] += (
                            np.ceil(change * 100000000000000)
                            / 100000000000000  # TODO: What is this??
                        )

        # Recalculate the support ratio
        total_support = sum(donor.get(chosen_project, 0) for donor in donors)
        r = total_support / project_cost
    return True


def reverse_eliminations(
    selected_projects: BudgetAllocation,
    donors: list[dict[Project, Numeric]],
    eliminated_projects: set[Project],
    project_to_fund_selection_procedure: Callable,
    budget: int,
    tie_breaking: TieBreakingRule = lexico_tie_breaking,
) -> None:
    """
    Reverses eliminations of projects if the budget allows.

    Parameters
    ----------
        selected_projects : BudgetAllocation
            The list of selected projects.
        eliminated_projects : Instance
            The list of eliminated projects.
        budget : int
            The remaining budget.

    Returns
    -------
        None
    """
    logger.debug("Performing inclusive maximality postprocedure RE")
    for project in eliminated_projects:
        if project.cost <= budget:
            selected_projects.append(project)
            budget -= project.cost


def acceptance_of_under_supported_projects(
    selected_projects: BudgetAllocation,
    donors: list[dict[Project, Numeric]],
    eliminated_projects: Instance,
    project_to_fund_selection_procedure: Callable,
    budget: Numeric,
    tie_breaking: TieBreakingRule = lexico_tie_breaking,
) -> None:
    """
    Accepts undersupported projects if the budget allows.

    Parameters
    ----------
        selected_projects : BudgetAllocation
            The list of selected projects.
        donors : list[dict[Project, Numeric]]
            The list of donor ballots.
        eliminated_projects : Instance
            The list of eliminated projects.
        project_to_fund_selection_procedure : callable
            The procedure to select a project for funding.
        budget : Numeric
            The remaining budget.
        tie_breaking : TieBreakingRule, optional
            The tie-breaking rule to use, defaults to lexico_tie_breaking.

    Returns
    -------
        None

    Examples
    --------
    >>> project_A = Project("Project A", 35)
    >>> project_B = Project("Project B", 30)
    >>> project_C = Project("Project C", 20)
    >>> S = Instance([project_A])
    >>> eliminated_projects = Instance([project_B, project_C])
    >>> sorted(acceptance_of_under_supported_projects(S, Profile([]), eliminated_projects, select_project_ge, 25, lexico_tie_breaking))
    [Project A, Project C]
    """
    logger.debug("Performing inclusive maximality postprocedure: AUP")
    while len(eliminated_projects) != 0:
        selected_project = project_to_fund_selection_procedure(
            eliminated_projects, donors, tie_breaking, True
        )[
            0
        ]  # TODO: tie-breaking here
        if selected_project.cost <= budget:
            selected_projects.append(selected_project)
            eliminated_projects.remove(selected_project)
            budget -= selected_project.cost
        else:
            eliminated_projects.remove(selected_project)