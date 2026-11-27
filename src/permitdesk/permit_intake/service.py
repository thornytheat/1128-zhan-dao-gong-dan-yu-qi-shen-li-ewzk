from collections import Counter
from .inbox import load_permits,route_aliases

def intake(updates,names,now):
    return load_permits(updates,route_aliases(names),now)

def district_evidence(register):
    """Report permits whose own visible revisions disagree about the district."""
    return [dict(row,conflict=True) for row in register.revisions if len(row["districts"])>1]

def material_workload(register):
    counts=Counter((row.district,row.work) for row in register.active)
    return [{"district":district,"type":work,"submitted":count} for (district,work),count in sorted(counts.items())]
