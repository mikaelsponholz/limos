These are invented demo people, companies and placeholder products. Pena and Ab Oy
are fictional demo identities, as are Alex Example and Bistro Example Oy. Every
profile and history entry is simulated. History prices are placeholder EUR values.

`python scripts/seed_customers.py --catalog <path to catalog.slim.json>` resolves
the history products offline into the git-ignored `.runtime/customers/` overlay.
It leaves these committed profiles unchanged. Search terms live separately in
`scripts/customer_seed_spec.json`. The current slim catalogue has no prices, so
resolved history retains the placeholder prices.
