# browser_headlines

A reference browser plan (`kind: "browser_plan"`, see [docs/browser-plans.md](../../../docs/browser-plans.md)).

The host `headlines.example.org` **does not exist**, and the pages in `fixtures/` are made up and written by hand: they are not
recordings of any site. The pack shows the shape of a plan and lets the gates run offline. It cannot read anything live, so it has no
`live` example. To use it for a real site, change the host, the anchors in `plan.json` and the recorded pages, and have an operator
approve the new host.
