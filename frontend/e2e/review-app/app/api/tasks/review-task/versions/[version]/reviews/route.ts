import { NextResponse } from "next/server";

export async function GET() {
  return NextResponse.json({
    content_hash: "fixture-content", content_algorithm: "sha256", task_version_id: "v2",
    current_user_id: "me", can_moderate: false, average_score: 3, rating_count: 1,
    own_rating: null, own_rating_revision: 0,
    ratings: [{ id: "rating", reviewer: { id: "other", name: "Example reviewer" }, score: 3, revision: 1, updated_at: "2026-10-08T17:00:00Z" }],
    comments: [{ id: "comment", reviewer: { id: "other", name: "Example reviewer" }, body: "The instructions need clearer acceptance criteria.", source_trial_id: null, revision: 1, created_at: "2026-10-08T17:00:00Z", updated_at: "2026-10-08T17:00:00Z" }],
    next_before: null, next_before_id: null,
  });
}
