# Review a delivery

A delivery groups tasks for shipment and records checks and human sign-off. You need access to its Oddish workspace. Reading these docs does not grant that access.

Replace `DELIVERY_ID` with the ID or unique name supplied by your workspace administrator:

```bash
oddish delivery show DELIVERY_ID
oddish delivery ready DELIVERY_ID
```

`show` lists tasks and blockers. `ready` exits with code `0` when every required check passes, or `1` when blockers remain. A successful QA verdict alone does not make a delivery ready.

## Approve a task

Changing a delivery requires an administrator role or a full-scope key. Review the task's current version and each reported defect before signing off:

```bash
oddish delivery signoff DELIVERY_ID TASK_ID
```

Replace `TASK_ID` with the reviewed task. If blockers exist, the command lists them and asks whether to acknowledge them. Confirming records those exceptions in your name. Do not confirm defects you have not reviewed.

A new task version needs a new review. Use `oddish delivery history TASK_ID` to inspect recorded reviews. After the complete delivery passes its checks, an authorized administrator can run `oddish delivery finalize DELIVERY_ID` to pin versions and freeze the record.
