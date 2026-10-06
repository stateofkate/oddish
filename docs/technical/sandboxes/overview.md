# Sandboxes

A sandbox is the isolated machine a trial runs in. Oddish can place a trial on several providers; which one depends on what the task needs and on rollout settings in the production deploy.

| Provider | Used for | Supervised by | Notes |
| --- | --- | --- | --- |
| Modal, CPU | most agent trials | oddish-mon for 70% of first attempts; the old watcher otherwise | the main population |
| Modal, GPU | GPU tasks | old watcher | Modal's VM-style sandbox cannot carry a GPU, so oddish-mon does not run there yet |
| Archil, CPU | 40% of new CPU submissions since 30 September | old watcher | no oddish-mon adapter; stays on the old path for now |
| Thunder | explicit GPU opt-in | old watcher | |
| GKE (TPU), EC2 | explicit choices | old watcher | |
| Daytona | none | n/a | all launches disabled on 28 September |

Review jobs (audit, QA, summaries) run on the old watcher path in an analysis sandbox regardless of provider.

## Reports

- [oddish-mon](oddish-mon/introduction.md): moving trial supervision inside the sandbox. Design, delivery, running it, roadmap and appendix.
