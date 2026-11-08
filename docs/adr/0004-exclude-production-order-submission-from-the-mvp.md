# Exclude production order submission from the MVP

The MVP may read a production Shioaji account and exercise order workflows only in Shioaji's simulation environment, but it will contain no callable production order-submission path. This deliberately delays integration completeness so an account eligibility gap, leaked feature flag, or operator mistake cannot bypass the agreed Shadow, Paper, reconciliation, and Live Canary promotion gates.
