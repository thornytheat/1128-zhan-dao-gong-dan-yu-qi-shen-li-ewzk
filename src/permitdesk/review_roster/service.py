from dataclasses import asdict
from .staffing import solve
from .shift_replay import shift_impact

def prepare_roster(cases,clocks,predicted,documents,config,capacity,permit_zones=None,reviewer_zones=None):
    return solve(cases,clocks,predicted,documents,config["reviewers"],capacity,config["at"],
                 permit_zones=permit_zones,reviewer_zones=reviewer_zones)

def hearing_records(orders):
    return [asdict(summary) for summary in orders]
