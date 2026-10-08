import { apiFetch } from "@/lib/api";

type Reviewer = { id: string; name: string };
type Rating = {
  id: string;
  reviewer: Reviewer;
  score: number;
  revision: number;
  updated_at: string;
};
export type ReviewComment = {
  id: string;
  reviewer: Reviewer;
  body: string;
  source_trial_id: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
};
export type ReviewPage = {
  content_hash: string;
  content_algorithm: string;
  task_version_id: string;
  current_user_id: string;
  can_moderate: boolean;
  average_score: number | null;
  rating_count: number;
  own_rating: Rating | null;
  own_rating_revision: number;
  ratings: Rating[];
  comments: ReviewComment[];
  next_before: string | null;
  next_before_id: string | null;
};

export async function reviewRequest<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await apiFetch(url, options);
  const body = await response.json();
  if (!response.ok) {
    if (response.status === 409)
      throw new Error(
        "This review changed. Reload reviews before saving again."
      );
    if (response.status === 404)
      throw new Error(
        "Reviews are unavailable while this version is being prepared."
      );
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : "Could not save or load reviews. Please try again."
    );
  }
  return body as T;
}

