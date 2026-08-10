"""
Reconciliation: compares our own ledger's record of transactions against
the switch's independently-produced settlement file, matched by RRN.

This runs on its own schedule (a Kubernetes CronJob, in production), never
triggered by any individual transaction. It's the only place in the whole
stack that checks our own beliefs against an external, independent source
of truth -- everything else only checks internal self-consistency.
"""

from dataclasses import dataclass, field


@dataclass
class ReconciliationResult:
    matched: list = field(default_factory=list)
    only_in_ledger: list = field(default_factory=list)         # we think it happened, switch has no record
    only_in_settlement: list = field(default_factory=list)     # switch says it happened, we have no record
    amount_mismatches: list = field(default_factory=list)      # both have it, amounts disagree

    @property
    def is_clean(self) -> bool:
        return not (self.only_in_ledger or self.only_in_settlement or self.amount_mismatches)

    def summary(self) -> str:
        return (
            f"{len(self.matched)} matched, "
            f"{len(self.only_in_ledger)} only in ledger, "
            f"{len(self.only_in_settlement)} only in settlement, "
            f"{len(self.amount_mismatches)} amount mismatches"
        )


def reconcile(ledger_conn, settlement_entries: list) -> ReconciliationResult:
    """
    settlement_entries: [{"rrn": str, "amount_cents": int}, ...] -- the
    switch's settlement file, however it was loaded (CSV, JSON, an API
    response -- the format doesn't matter here, only the shape).
    """
    result = ReconciliationResult()

    ledger_rows = ledger_conn.execute("SELECT rrn, amount_cents FROM transactions").fetchall()
    ledger_by_rrn = {rrn: amount for rrn, amount in ledger_rows}

    settlement_by_rrn = {e["rrn"]: e["amount_cents"] for e in settlement_entries}

    all_rrns = set(ledger_by_rrn) | set(settlement_by_rrn)

    for rrn in sorted(all_rrns):
        in_ledger = rrn in ledger_by_rrn
        in_settlement = rrn in settlement_by_rrn

        if in_ledger and in_settlement:
            if ledger_by_rrn[rrn] == settlement_by_rrn[rrn]:
                result.matched.append(rrn)
            else:
                result.amount_mismatches.append({
                    "rrn": rrn,
                    "ledger_amount_cents": ledger_by_rrn[rrn],
                    "settlement_amount_cents": settlement_by_rrn[rrn],
                })
        elif in_ledger and not in_settlement:
            result.only_in_ledger.append(rrn)
        elif in_settlement and not in_ledger:
            result.only_in_settlement.append(rrn)

    return result
