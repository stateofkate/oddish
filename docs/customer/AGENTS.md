# Oddish customer guides

These Markdown files are the maintained source for the Oddish section of abundant-ai/abundant-docs and for the Docs page in the Oddish app (frontend/docs-bundle.mjs inlines them at build; frontend/src/components/docs-view.tsx lists which pages each kind of org sees). The website is in that separate repository. Only navigation.json pages and reviewed assets are imported. This AGENTS.md is not a customer page.

Write for a reader without repository context. Define terms, give exact actions and expected results, and use fictional examples. Update these guides in the same PR as customer-visible behavior. Keep the packaged CLI skill current. Guides for organization admins, such as labeling QA goldens, belong here. Do not put Abundant operator runbooks (deploys, migrations, provider setup), private task data, credentials or internal screenshots here.

The documentation platform fetches published source changes independently. No website build, deploy key, docs checkout, or required docs CI job belongs in Oddish.

The guide for organizations Abundant shares data with is in docs/shared, a separate collection (Shared data) so those readers can be granted only that page.
