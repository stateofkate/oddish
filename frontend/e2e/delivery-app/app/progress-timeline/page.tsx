import { HarborStageTimeline } from "../../../../src/components/harbor-stage-timeline";

const ended = {
  started_at: "2026-10-06T12:00:00Z",
  finished_at: "2026-10-06T12:01:00Z",
  duration_sec: 60,
};
export default function Page() {
  return (
    <main>
      <section aria-label="No stage"><HarborStageTimeline currentStage={null} status="running" /></section>
      <section aria-label="Before provisioning failure">
        <HarborStageTimeline
          currentStage="completed"
          status="failed"
          isFailure
          phaseTiming={null}
        />
      </section>
      <section aria-label="Failure after setup">
        <HarborStageTimeline
          currentStage="completed"
          status="failed"
          phaseTiming={{ environment_setup: ended }}
        />
      </section>
      <section aria-label="Failure after agent">
        <HarborStageTimeline
          currentStage="completed"
          status="failed"
          phaseTiming={{ environment_setup: ended, agent_execution: ended }}
        />
      </section>
      <section aria-label="Successful trial">
        <HarborStageTimeline currentStage="completed" status="success" />
      </section>
    </main>
  );
}
