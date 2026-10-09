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
  task_version_id: string;
  current_user_id: string;
  can_moderate: boolean;
  average_score: number | null;
  rating_count: number;
  own_rating: Rating | null;
  own_rating_revision: number;
  ratings: Rating[];
  comments: ReviewComment[];
  own_comment: ReviewComment | null;
  comment_count: number;
  next_before: string | null;
  next_before_id: string | null;
};

export async function reviewRequest<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await apiFetch(url, options);
  if (!response.ok)
    throw new Error(options?.method ? "Couldn’t save review." : "Couldn’t load reviews.");
  return await response.json() as T;
}
