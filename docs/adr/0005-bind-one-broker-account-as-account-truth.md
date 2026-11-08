# Bind one broker account as account truth

The platform will explicitly bind exactly one Shioaji Strategy Account and treat its reconciled read-only positions, cash, settlements, and events as Observed Account State. Configured planning NAV remains available only in visibly distinct Planning Mode. Unknown instruments are retained and labelled as external holdings: the platform neither sells nor takes control of them, and excludes them from strategy exposure. Financing, short positions, non-cash structures, or failed reconciliation of allowed strategy instruments remain Broker Unsafe because convenience is not worth ambiguous authority or incorrect Actual Exposure.

## Consequences

A missing, duplicated, mismatched, or unreconciled account is Broker Unsafe and fails closed. Rebinding requires explicit confirmation, discards no history, and forces a fresh full reconciliation before Observed Account State becomes usable again.
