# Prepare a lab delivery

A delivery is a named batch of Oddish tasks for one lab, such as GDM. Oddish calls the lab the delivery's customer. This page shows how to pick the tasks and put them in a delivery, on the Tasks page or with the `oddish` command-line tool.

## Which tasks to pick

Pick a task only when all three are true:

| Condition | What it means | Filter |
| --- | --- | --- |
| It is in a registry repo | Its files are on the `main` branch of harbor-lh, harbor-forge-v2, harbor-forge-archive, smithy or sre-world. Oddish tags these tasks `registry-main` and tags newly merged tasks within an hour. Untagged tasks are older copies or uploads that never reached `main`. | Tag `registry-main` |
| It passed QA | The QA review of its current version accepted it. | QA outcome Accepted |
| The lab has not received it | Oddish has no record of sending it to that lab. | Not delivered to the lab |

Oddish knows about a delivery only if it was made in Oddish or imported into its delivery history. A task sent some other way still counts as not received.

Adding tasks to a delivery requires the admin role in the Oddish organization, or an API key with full access.

## On the Tasks page

1. Open https://www.oddish.app/orgs/abundant/tasks. The filters are in the left column.
2. Under **Review**, tick **Accepted** and nothing else.
3. Under **Delivery history**, set the first dropdown to the lab (**GDM**) and the second to **No record**.
4. Open **Additional filters**. Under **Tags**, click **Has all**, open the list below it, and tick **registry-main**.
5. Optional, newest first: under **Sort**, choose **Created**, and click the arrow next to it until it shows ↓.
6. Tick the tasks you want, or click **Select all N matching tasks** in the bar above the results, next to the task count.
7. In the bar at the bottom left, click **Add to delivery**. Under **Delivery batch**, choose the delivery, or click **New delivery…**, enter a name, and choose **GDM** as the customer. Click **Add N tasks**.

The delivery now lists the tasks at https://www.oddish.app/orgs/abundant/deliveries.

## With the command line

The tool must be installed and `ODDISH_API_KEY` set to your key. `oddish delivery customers` lists the lab names Oddish uses: GDM, Nvidia, TML and Internal QA.

1. Count the tasks that qualify:

   ```bash
   oddish ls --tag registry-main --qa-outcome accepted --not-delivered-to GDM --count
   ```

   It prints one number.

2. To keep only recent tasks, add `--created-within 7d` (or `24h`, `30d`, `90d`):

   ```bash
   oddish ls --tag registry-main --qa-outcome accepted --not-delivered-to GDM --created-within 7d --count
   ```

3. List them, 25 per page, most recently run first. Add `--offset 25` for the next page:

   ```bash
   oddish ls --tag registry-main --qa-outcome accepted --not-delivered-to GDM --created-within 7d --limit 25
   ```

4. Save every matching task ID to a file (at most 5,000):

   ```bash
   oddish ls --tag registry-main --qa-outcome accepted --not-delivered-to GDM --created-within 7d --ids --json > gdm-tasks.json
   ```

5. Put them in a delivery. For a new delivery:

   ```bash
   oddish delivery create "GDM October batch" --customer GDM --tasks-file gdm-tasks.json
   ```

   For a delivery that already exists, find its name with `oddish delivery list`, then:

   ```bash
   oddish delivery add "GDM October batch" --tasks-file gdm-tasks.json
   ```

6. Check the result:

   ```bash
   oddish delivery show "GDM October batch"
   ```

   It lists every task in the delivery.

## After the tasks are in

`oddish delivery ready "GDM October batch"` lists what still blocks the delivery. `oddish delivery signoff "GDM October batch" TASK_ID` records your review of one task. When nothing blocks it, an admin runs `oddish delivery finalize "GDM October batch"`, which freezes the task list and versions.
