# Update the QA golden set

A trial is one agent's attempt at one task. After a trial finishes, Oddish's QA step reads it and gives it one classification:

| Classification  | Meaning                                                                 |
| --------------- | ----------------------------------------------------------------------- |
| `GOOD_SUCCESS`  | The agent passed, and the pass is legitimate.                           |
| `GOOD_FAILURE`  | The agent failed a fair requirement.                                    |
| `BAD_SUCCESS`   | The agent passed, but the pass is invalid (for example, weak tests).    |
| `BAD_FAILURE`   | A defect in the task caused the failure.                                |
| `HARNESS_ERROR` | The machines running the trial broke, so the score means nothing.      |

A golden is the classification a person decided is correct for one trial. The golden set is every golden in your workspace. Oddish measures QA's accuracy by comparing QA's classification with the golden on each golden trial.

Each golden has one state. Only `counted` goldens are used to measure accuracy.

| State     | Meaning                                                            |
| --------- | ------------------------------------------------------------------ |
| `counted` | A person reviewed it, and the task's files have not changed since. |
| `waiting` | It was imported from a file and no person has reviewed it yet.     |
| `stale`   | The task's files changed after the golden was reviewed.            |

## Before you start

- Install the `oddish` command-line tool ([Getting started](../getting-started.md)).
- Set `ODDISH_API_KEY` to a full-scope key belonging to an admin of your organization. Commands that read or change goldens are refused for any other key.

Every command below prints a link as its last line. The link opens the same list in the Oddish website.

## Label one trial

1. List your experiments (an experiment is a named group of trials):

   ```bash
   oddish qa goldens experiments
   ```

   Each line is: experiment ID, name, author, date, `N failed / M trials`. Add `--member all` to list every member's experiments.

2. List the experiment's finished trials that failed and have no counted golden. Replace `EXPERIMENT_ID` with an ID from step 1:

   ```bash
   oddish qa goldens trials EXPERIMENT_ID
   ```

   Each line is: trial ID, task name, reward (the task's test score: `1` passed, `0` failed), QA's classification, and the golden (`unlabeled` if none). Add `--queue passed` for passed trials, `--queue labeled` for trials that already have a counted golden, or `--queue all` for every trial.

3. Save the correct classification. Replace `TRIAL_ID` and `CLASSIFICATION`, and write your reason in the note:

   ```bash
   oddish qa goldens label TRIAL_ID CLASSIFICATION --note "The tests check a file the instructions never mention."
   ```

   The golden is `counted` immediately. If the trial already had a golden with a different classification, the old golden is removed and the new one replaces it.

4. Confirm:

   ```bash
   oddish qa goldens list --trial TRIAL_ID
   ```

   Success is one line: golden ID, source, trial ID, classification. If a state word (`waiting` or `stale`) follows the classification, the golden is not counted.

## Fix stale goldens

1. List trials whose golden is stale:

   ```bash
   oddish qa goldens trials --review stale
   ```

2. For each trial, check the classification against the task's current files, then run `label` with the classification that is correct now:

   ```bash
   oddish qa goldens label TRIAL_ID CLASSIFICATION
   ```

   Running `label` with the same classification as before marks the golden `counted` again against the current files. Running it with a different classification replaces the golden.

## Review waiting goldens

1. List waiting goldens. The first column is the golden ID:

   ```bash
   oddish qa goldens list --waiting
   ```

2. For each golden whose classification is correct, mark it reviewed. It becomes `counted`:

   ```bash
   oddish qa goldens review GOLDEN_ID
   ```

   For a wrong one, run `oddish qa goldens label TRIAL_ID CLASSIFICATION` with the correct classification instead.

## Remove a golden

```bash
oddish qa goldens retire GOLDEN_ID
```

The golden stops counting. The trial can then be labeled again.

## Add many goldens from a file

1. Write a CSV file with one row per trial and these columns:

   ```text
   source_trial_id,expected_classification,note
   TRIAL_ID,GOOD_FAILURE,The agent skipped the required output file.
   ```

2. Preview. Replace `BATCH_NAME` with a name for this file's labels; it is stored on each golden as its source:

   ```bash
   oddish qa goldens import labels.csv --source BATCH_NAME --review-all --dry-run
   ```

   The preview prints how many rows will be added and how many are refused. It changes nothing.

3. Import by running the same command without `--dry-run`.

   `--review-all` makes every imported golden `counted`. Use it only if a person checked every row. Without it, imported goldens are `waiting`.

   A row whose trial already has a golden with a different classification is refused, and the command exits with code 1. To change that trial's golden, use `label`.

4. Confirm with `oddish qa goldens list --source BATCH_NAME`.
