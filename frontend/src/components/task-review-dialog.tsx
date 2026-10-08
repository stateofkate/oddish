"use client";

// Extend the existing compact Oddish controls: review one task version without
// changing the task layout, automated QA, or customer-facing pages.
import { useState } from "react";
import useSWRInfinite from "swr/infinite";
import { MessageSquare } from "lucide-react";
import {
  reviewRequest as request,
  type ReviewPage,
  type ReviewComment,
} from "@/lib/task-reviews";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

type Version = { id: string; version: number; is_current: boolean };

export function TaskReviewDialog({
  taskId,
  taskName,
  versions,
  selectedVersionId,
}: {
  taskId: string;
  taskName: string;
  versions: Version[];
  selectedVersionId: string;
}) {
  const [open, setOpen] = useState(false);
  const [versionId, setVersionId] = useState(selectedVersionId);
  const [busy, setBusy] = useState(false);
  return (
    <Dialog
      open={open}
      onOpenChange={(value) => {
        if (busy) return;
        if (value) setVersionId(selectedVersionId);
        setOpen(value);
      }}
    >
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <MessageSquare aria-hidden />
          Review task
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Review task</DialogTitle>
          <DialogDescription>{taskName} · Internal</DialogDescription>
        </DialogHeader>
        <label className="flex items-center gap-3 text-sm">
          Version
          <select
            aria-label="Review version"
            value={versionId}
            disabled={busy}
            onChange={(event) => setVersionId(event.target.value)}
            className="border-input bg-background rounded-md border px-3 py-2"
          >
            {versions.map((version) => (
              <option key={version.id} value={version.id}>
                v{version.version}
                {version.is_current ? " · Default" : ""}
              </option>
            ))}
          </select>
        </label>
        {open && (
          <ReviewForm
            key={`${taskId}:${versionId}`}
            taskId={taskId}
            versionId={versionId}
            onBusy={setBusy}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

function ReviewForm({
  taskId,
  versionId,
  onBusy,
}: {
  taskId: string;
  versionId: string;
  onBusy: (busy: boolean) => void;
}) {
  const base = `/api/tasks/${encodeURIComponent(taskId)}/versions/${encodeURIComponent(versionId)}/reviews`;
  const {
    data: pages,
    error,
    isLoading,
    size,
    setSize,
    mutate,
  } = useSWRInfinite<ReviewPage>(
    (index, previous: ReviewPage | null) => {
      if (index === 0) return base;
      if (!previous?.next_before || !previous.next_before_id) return null;
      return `${base}?${new URLSearchParams({ before: previous.next_before, before_id: previous.next_before_id })}`;
    },
    request<ReviewPage>,
    { revalidateOnFocus: false }
  );
  const data = pages?.[0];
  const [score, setScore] = useState<number | null | undefined>(undefined);
  const [body, setBody] = useState("");
  const [requestId, setRequestId] = useState<string | null>(null);
  const [editing, setEditing] = useState<ReviewComment | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [failure, setFailure] = useState("");
  const chosenScore =
    score === undefined ? (data?.own_rating?.score ?? null) : score;
  async function write(
    path: string,
    method: string,
    payload: object,
    complete: () => void
  ) {
    if (!data || busy) return;
    setBusy(true);
    onBusy(true);
    setFailure("");
    setMessage("");
    try {
      await request(base + path, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content_hash: data.content_hash,
          content_algorithm: data.content_algorithm,
          ...payload,
        }),
      });
      complete();
      await mutate();
      setMessage("Saved");
    } catch (error) {
      setFailure(
        error instanceof Error ? error.message : "Could not save review."
      );
    } finally {
      setBusy(false);
      onBusy(false);
    }
  }
  if (isLoading)
    return (
      <p role="status" className="text-muted-foreground text-sm">
        Loading reviews…
      </p>
    );
  if (!data)
    return (
      <div role="alert" className="space-y-2 text-sm">
        <p>{error?.message ?? "Could not load reviews."}</p>
        <Button variant="outline" onClick={() => void mutate()}>
          Try again
        </Button>
      </div>
    );
  const comments = [
    ...new Map(
      (pages ?? [])
        .flatMap((page) => page.comments)
        .map((comment) => [comment.id, comment])
    ).values(),
  ];
  return (
    <div className="space-y-4 text-sm">
      <div>
        <strong>
          Task quality{" "}
          {data.average_score == null
            ? "Not rated"
            : `${data.average_score.toFixed(1)} / 5`}
        </strong>
        <p className="text-muted-foreground">
          {data.rating_count === 0
            ? "No ratings yet"
            : `${data.rating_count} ${data.rating_count === 1 ? "rating" : "ratings"}`}{" "}
          · Rate this version and leave feedback.
        </p>
      </div>
      <fieldset disabled={busy} className="space-y-2">
        <legend className="mb-2 font-medium">Your rating</legend>
        <div className="flex flex-wrap gap-2">
          {[1, 2, 3, 4, 5].map((value) => (
            <Button
              key={value}
              aria-label={`Rate ${value} out of 5`}
              aria-pressed={chosenScore === value}
              variant="outline"
              className={chosenScore === value ? "border-foreground font-semibold ring-1 ring-foreground" : undefined}
              size="sm"
              onClick={() => setScore(value)}
            >
              {value}
            </Button>
          ))}
        </div>
        <p className="text-muted-foreground text-xs">
          1 · Poor 3 · Adequate 5 · Excellent
        </p>
        <div className="flex gap-2">
          <Button
            size="sm"
            disabled={
              chosenScore === null ||
              chosenScore === (data.own_rating?.score ?? null)
            }
            onClick={() =>
              void write(
                "/rating",
                "PUT",
                {
                  score: chosenScore,
                  expected_revision: data.own_rating_revision,
                },
                () => setScore(undefined)
              )
            }
          >
            Save rating
          </Button>
          {data.own_rating && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() =>
                void write(
                  "/rating",
                  "PUT",
                  { score: null, expected_revision: data.own_rating_revision },
                  () => setScore(undefined)
                )
              }
            >
              Remove rating
            </Button>
          )}
        </div>
      </fieldset>
      <form
        className="space-y-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (!body.trim()) return;
          const id = requestId ?? crypto.randomUUID();
          setRequestId(id);
          void write(
            editing
              ? `/comments/${encodeURIComponent(editing.id)}`
              : "/comments",
            editing ? "PATCH" : "POST",
            editing
              ? { body: body.trim(), expected_revision: editing.revision }
              : { body: body.trim(), client_request_id: id },
            () => {
              setBody("");
              setRequestId(null);
              setEditing(null);
            }
          );
        }}
      >
        <label htmlFor="task-review-comment" className="font-medium">
          {editing ? "Edit comment" : "Comment"}
        </label>
        <Textarea
          id="task-review-comment"
          placeholder="Add feedback on this task…"
          maxLength={10000}
          value={body}
          disabled={busy}
          onChange={(event) => {
            setBody(event.target.value);
            setRequestId(null);
          }}
        />
        <div className="flex gap-2">
          <Button size="sm" disabled={busy || !body.trim()}>
            {editing ? "Save comment" : "Post comment"}
          </Button>
          {editing && (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => {
                setEditing(null);
                setBody("");
              }}
            >
              Cancel edit
            </Button>
          )}
        </div>
      </form>
      {message && <p role="status">{message}</p>}
      {(failure || error) && (
        <div role="alert">
          <p>{failure || error?.message}</p>
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() => {
              setFailure("");
              void mutate();
            }}
          >
            Reload reviews
          </Button>
        </div>
      )}
      <section className="space-y-2 border-t pt-4" aria-label="Ratings">
        <h3 className="font-medium">Ratings</h3>
        {data.ratings.map((rating) => (
          <div className="flex flex-wrap justify-between gap-2" key={rating.id}>
            <span>
              {rating.reviewer.name}
              {rating.reviewer.id === data.current_user_id ? " (you)" : ""}
            </span>
            <span className="font-mono tabular-nums">{rating.score} / 5</span>
          </div>
        ))}
        {data.rating_count > data.ratings.length && (
          <p className="text-muted-foreground">
            Showing the latest {data.ratings.length} ratings.
          </p>
        )}
      </section>
      <section className="space-y-3 border-t pt-4" aria-label="Comments">
        <h3 className="font-medium">Comments</h3>
        {comments.length === 0 && (
          <p className="text-muted-foreground">No comments yet.</p>
        )}
        {comments.map((comment) => (
          <article key={comment.id} className="space-y-1 border-b pb-3">
            <strong>
              {comment.reviewer.name}
              {comment.reviewer.id === data.current_user_id ? " (you)" : ""}
            </strong>
            <p className="text-muted-foreground text-xs">
              <time dateTime={comment.created_at}>
                {new Date(comment.created_at).toLocaleString()}
              </time>
              {comment.revision > 1 ? " · Edited" : ""}
            </p>
            <p className="break-words whitespace-pre-wrap">{comment.body}</p>
            {comment.source_trial_id && (
              <p className="text-muted-foreground text-xs break-all">
                Trial: {comment.source_trial_id}
              </p>
            )}
            {(comment.reviewer.id === data.current_user_id ||
              data.can_moderate) && (
              <div className="flex gap-2">
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={busy}
                  onClick={() => {
                    setEditing(comment);
                    setBody(comment.body);
                    setRequestId(null);
                  }}
                >
                  Edit
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={busy}
                  onClick={() => {
                    if (window.confirm("Delete this comment?"))
                      void write(
                        `/comments/${encodeURIComponent(comment.id)}`,
                        "PATCH",
                        { body: null, expected_revision: comment.revision },
                        () => {
                          if (editing?.id === comment.id) {
                            setEditing(null);
                            setBody("");
                          }
                        }
                      );
                  }}
                >
                  Delete
                </Button>
              </div>
            )}
          </article>
        ))}
        {pages?.at(-1)?.next_before && (
          <Button
            variant="outline"
            size="sm"
            disabled={busy || pages.length < size}
            onClick={() => void setSize(size + 1)}
          >
            Older comments
          </Button>
        )}
      </section>
    </div>
  );
}
