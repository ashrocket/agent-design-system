# Research state

This directory holds the queue, budget policy, append-only ledger, and generated
results for the optional nightly research harness.

The checked-in ledger begins with the first local bootstrap attempt. It failed
before model usage, but the harness conservatively charged the full reservation
and retained the result envelope instead of silently resetting the budget. On
GitHub, the workflow keeps later ledger entries and results on the public
`automation/research-state` branch and opens or updates a pull request for
review.

The default 9,000-token reservation permits one run per UTC day, at most 63,000
reserved tokens in a calendar week and 279,000 in a 31-day month. The configured
ceilings leave modest accounting headroom while keeping the calculation easy to
inspect. Change the numbers in `budget.json` to match the project's own limit;
they are not linked to an OpenAI billing limit.
