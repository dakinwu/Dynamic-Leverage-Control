# Separate alert acknowledgement from resolution

Operator Queue Acknowledgement records only that a person has seen an item; it cannot resolve the item, lower Operator Status, or override the Risk Gate. Resolution is produced only when evidence proves the underlying condition has cleared, trading the convenience of manual dismissal for an auditable safety model that cannot be bypassed from the dashboard or Telegram.

## Consequences

Queue items use an immutable Open, Acknowledged, Resolved, and Reopened lifecycle. Every transition records its actor, time, reason, and evidence, and repeated occurrences remain correlated without being hidden or deleted.
