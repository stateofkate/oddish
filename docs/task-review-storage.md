# Internal task reviews

Reviews belong to an organization, reviewer and task version through foreign keys.
They do not depend on task files, expansion jobs or archive fingerprints, and are
never exposed on customer share pages.

`task_review_ratings` stores a 1–5 score with one current rating per reviewer and
version. `task_review_comments` stores comments, with an optional source-trial FK.
Both retain revisions, timestamps and soft deletion. The API checks organization
access, edit ownership and the supplied revision. Comment creation uses a request
UUID to prevent duplicate posts.

Migration `task_reviews_version_002` removes the fingerprint requirement while
retaining existing rows and legacy metadata. If one reviewer rated several content
snapshots of a version, their latest action remains current; older rating rows are
retained as cleared history. Comments remain intact. Legacy metadata columns stay
nullable for deployment compatibility, but application code no longer uses them.
