"""Frozen v1 cost-summary DDL, shared by migrated and create_all databases.

Change this contract through a new version and migration, not by editing v1.
"""

COST_SUMMARY_DDL = """
CREATE OR REPLACE FUNCTION task_cost_summary_v1(vid text) RETURNS jsonb
LANGUAGE sql STABLE AS $$
 WITH subject AS (
   SELECT v.id, v.task_id, v.created_at, t.org_id FROM task_versions v
   JOIN tasks t ON t.id=v.task_id WHERE v.id=vid
 ), models AS (
   SELECT tr.cost_usd FROM trials tr JOIN subject s ON s.id=tr.task_version_id
   WHERE tr.org_id IS NOT DISTINCT FROM s.org_id
     AND tr.deleted_at IS NULL AND tr.superseded_by_trial_id IS NULL
     AND tr.kind='agent'
     AND (tr.idempotency_key IS NULL OR tr.idempotency_key NOT LIKE 'combine:%')
     AND coalesce(lower(tr.agent),'') !~ '^(oracle$|oracle-|agent-oracle|nop$|nop-|agent-nop)'
     AND ((tr.status='SUCCESS' AND tr.error_message IS NULL)
       OR (tr.status IN ('SUCCESS','FAILED') AND tr.reward IS NOT NULL
         AND (tr.error_message LIKE '%AgentTimeoutError%'
           OR tr.error_message LIKE '%Agent execution timed out%')))
 ), qa AS (
   SELECT CASE WHEN tr.status IN ('SUCCESS','FAILED') THEN tr.cost_usd END AS cost_usd FROM trials tr JOIN subject s ON s.id=tr.task_version_id
   WHERE tr.org_id IS NOT DISTINCT FROM s.org_id AND tr.deleted_at IS NULL
     AND tr.kind IN ('qa','audit','summarize')
     AND tr.status <> 'SKIPPED'
     AND (tr.idempotency_key IS NULL OR tr.idempotency_key NOT LIKE 'combine:%')
   UNION ALL
   SELECT a.cost_usd FROM analysis_costs a JOIN trials tr ON tr.id=a.trial_id
   JOIN subject s ON s.id=tr.task_version_id
   WHERE a.deleted_at IS NULL AND a.org_id IS NOT DISTINCT FROM s.org_id
     AND lower(a.job_kind) <> 'qa_eval'
 ), legacy_candidates AS (
   SELECT a.cost_usd, a.created_at, a.job_kind, a.org_id FROM subject s
   JOIN analysis_costs a ON a.task_id=s.task_id
   LEFT JOIN trials tr ON tr.id=a.trial_id
   WHERE a.deleted_at IS NULL AND tr.task_version_id IS NULL
   UNION ALL
   SELECT a.cost_usd, a.created_at, a.job_kind, a.org_id FROM subject s
   JOIN trials tr ON tr.task_id=s.task_id AND tr.task_version_id IS NULL
   JOIN analysis_costs a ON a.trial_id=tr.id
   WHERE a.deleted_at IS NULL AND a.task_id IS NULL
 ), legacy_unknown AS (
   SELECT count(*) AS n FROM legacy_candidates a CROSS JOIN subject s
   WHERE (a.cost_usd IS NULL OR a.cost_usd<>0)
     AND a.created_at >= s.created_at AND lower(a.job_kind) <> 'qa_eval'
     AND a.org_id IS NOT DISTINCT FROM s.org_id
 )
 SELECT jsonb_build_object(
   'completed_trials', (SELECT count(*) FROM models),
   'trial_sum', (SELECT coalesce(sum(cost_usd) FILTER (WHERE cost_usd>=0 AND cost_usd<'Infinity'::float8),0) FROM models),
   'trial_count', (SELECT count(*) FILTER (WHERE cost_usd>=0 AND cost_usd<'Infinity'::float8) FROM models),
   'qa_sum', (SELECT coalesce(sum(cost_usd) FILTER (WHERE cost_usd>=0 AND cost_usd<'Infinity'::float8),0) FROM qa),
   'qa_jobs', (SELECT count(*) FROM qa),
   'qa_unpriced_jobs', (SELECT count(*) FILTER (WHERE cost_usd IS NULL OR cost_usd<0 OR NOT cost_usd<'Infinity'::float8) FROM qa),
   'qa_unattributed_jobs', (SELECT n FROM legacy_unknown)
 )
$$;
CREATE OR REPLACE FUNCTION fill_task_cost_summary_v1() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 NEW.effort = coalesce(NEW.effort,'{}'::jsonb) ||
   jsonb_build_object('cost', task_cost_summary_v1(NEW.task_version_id));
 RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER task_effort_z_cost_compat
 BEFORE INSERT OR UPDATE OF effort ON task_version_browse_summaries
 FOR EACH ROW EXECUTE FUNCTION fill_task_cost_summary_v1();
CREATE OR REPLACE FUNCTION refresh_legacy_task_cost_v1() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE vid text;
BEGIN
 FOR vid IN SELECT v.id FROM task_versions v WHERE
   v.id IN (SELECT task_version_id FROM trials WHERE id IN (NEW.trial_id, OLD.trial_id))
   OR v.task_id IN (NEW.task_id, OLD.task_id)
   ORDER BY v.id
 LOOP
   PERFORM pg_advisory_xact_lock(hashtextextended(vid,0));
   UPDATE task_version_browse_summaries SET effort=coalesce(effort,'{}'::jsonb)
     WHERE task_version_id=vid;
 END LOOP;
 RETURN NULL;
END $$;
CREATE OR REPLACE TRIGGER task_cost_legacy_v1
 AFTER INSERT OR UPDATE OR DELETE ON analysis_costs
 FOR EACH ROW EXECUTE FUNCTION refresh_legacy_task_cost_v1();
"""

COST_SUMMARY_STATEMENTS = tuple(
    part if i == 0 else "CREATE " + part
    for i, part in enumerate(COST_SUMMARY_DDL.split(";\nCREATE "))
)
