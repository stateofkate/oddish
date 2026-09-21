# Private CI image

`ghcr.io/abundant-ai/oddish-ci-base-private` contains Python 3.13, build tools,
project environments under `/opt/venvs`, cached dependencies, and server source
under `/opt/warm`. Both the runnable image and the `buildcache` tag must stay
private. Oddish is the only publisher; `oddish-private` and `payments-platform`
consume this image and must not publish their own source to it.

## Access

In the package's **Manage Actions access**, grant `oddish` publishing access
and the two consuming repositories **Read**. Workflows authenticate with their
own `GITHUB_TOKEN`: the publisher needs `packages: write`, while consumers need
`packages: read` plus `container.credentials`. A login step inside a container
job is too late because GitHub must pull the image before executing that step.
Do not put `ODDISH_READ_TOKEN` into registry configuration.

The publisher checks package visibility through GitHub's API before building
or uploading source and fails unless it is `private`. If the package needs to
be recreated, initialize it with a source-free image, set and verify Private
in package settings, then run the publisher. Do not assume a new package's
visibility from its name or from the repository's settings.

While Oddish is public, GitHub warns that giving its workflows private-package
access can also give fork workflows access. The image is not a confidentiality
boundary against fork workflows until repository access and permitted fork
workflows have been restricted; review that policy at the private-repository
cutover. Never approve an untrusted workflow to download private server source.

## Build and validate

Run `CI Base Image` manually on the intended branch. Scheduled builds use the
default branch (`staging`); lockfile/Dockerfile pushes to `main` also rebuild.
The workflow publishes `latest`, a commit tag, and `buildcache`; a separate
fresh runner then pulls the exact published digest, synchronizes both project
environments, imports backend dependencies, and checks CLI startup.

Before switching another repository, grant its package access and run its
`CI Base Image Check` workflow. This checks a real job-container pull and that
repository's dependency lockfile without production secrets or deployments.
Also verify anonymous registry requests are denied for the image and cache.

## Migration and recovery

Publish and validate the new image before merging consumer changes. Apply the
Oddish changes to `staging` through a feature PR and then promote normally to
`main`; otherwise one publisher branch can keep updating the public package.
Record a working private digest so consumers can be pinned to it if a future
weekly build fails. Keep the old package until supported consumers and running
jobs have moved; then remove its source-containing versions and cache, including
untagged versions. Historical downloaded copies cannot be recalled.

Changing CI images does not itself deploy a production worker. Merging or
promoting code can trigger deployment workflows, so use the normal deployment
process and verify preview/staging before production promotion.
