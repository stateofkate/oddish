"use client";

// Compact Oddish controls: one rating/comment composer and one save.
// Existing theme tokens and solid filled selections keep the state clear.
import { useState, type ReactNode } from "react";
import useSWRInfinite from "swr/infinite";
import { MessageSquare } from "lucide-react";
import {
  reviewRequest as request,
  type ReviewPage,
  type ReviewComment,
} from "@/lib/task-reviews";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

type Draft = {
  score: number | null;
  body: string;
  editing: ReviewComment | null;
  requestId: string | null;
};
const reviewViews = { write: "write", reviews: "reviews" } as const;
const reviewTab =
  "rounded-none border-b-2 border-transparent bg-transparent px-0 py-2 text-sm shadow-none transition-none data-[state=active]:border-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none";

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
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
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
      <DialogContent className="flex h-[min(28rem,calc(100dvh-2rem))] w-[calc(100%-2rem)] flex-col gap-4 overflow-hidden sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Review task</DialogTitle>
          <DialogDescription>{taskName} · Internal</DialogDescription>
        </DialogHeader>
        {open && (
          <ReviewForm
            key={`${taskId}:${versionId}`}
            taskId={taskId}
            versionId={versionId}
            onBusy={setBusy}
            draft={drafts[versionId]}
            onDraft={(draft) =>
              setDrafts((current) => {
                const next = { ...current };
                if (draft) next[versionId] = draft;
                else delete next[versionId];
                return next;
              })
            }
            versionControl={
              <Select
                value={versionId}
                disabled={busy}
                onValueChange={setVersionId}
              >
                <SelectTrigger
                  aria-label="Review version"
                  className="w-auto min-w-28 gap-2"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {versions.map((version) => (
                    <SelectItem key={version.id} value={version.id}>
                      v{version.version}
                      {version.is_current ? " · Default" : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            }
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
  draft,
  onDraft,
  versionControl,
}: {
  taskId: string;
  versionId: string;
  onBusy: (busy: boolean) => void;
  draft?: Draft;
  onDraft: (draft?: Draft) => void;
  versionControl: ReactNode;
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
    { revalidateOnFocus: false, shouldRetryOnError: false }
  );
  const data = pages?.[0];
  const [view, setView] = useState<string>(reviewViews.write);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const ownComment =
    data?.own_comment ??
    data?.comments.find(
      (comment) => comment.reviewer.id === data.current_user_id
    ) ??
    null;
  const editing = draft?.editing ?? ownComment;
  const chosenScore = draft ? draft.score : (data?.own_rating?.score ?? null);
  const body = draft?.body ?? editing?.body ?? "";
  const unchanged =
    !!editing &&
    chosenScore === data?.own_rating?.score &&
    body.trim() === editing.body;
  const updateDraft = (change: Partial<Draft>) =>
    onDraft({
      score: chosenScore,
      body,
      editing,
      requestId: null,
      ...draft,
      ...change,
    });
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
    try {
      await request(base + path, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      complete();
      await mutate().catch(() => undefined);
    } catch {
      setFailure("Couldn’t save review.");
      const refreshed = await mutate().catch(() => undefined);
      if (editing && refreshed) {
        const latest = refreshed
          .flatMap((page) => [
            ...page.comments,
            ...(page.own_comment ? [page.own_comment] : []),
          ])
          .find((comment) => comment.id === editing.id);
        onDraft({
          score: chosenScore,
          body,
          requestId: null,
          editing: latest ?? null,
        });
      }
    } finally {
      setBusy(false);
      onBusy(false);
    }
  }
  if (!data)
    return (
      <>
        <div className="flex justify-end border-b pb-2">{versionControl}</div>
        <p
          role={isLoading && !error ? "status" : "alert"}
          className="text-muted-foreground text-sm"
        >
          {isLoading && !error ? "Loading reviews…" : "Couldn’t load reviews."}
        </p>
      </>
    );
  const comments = [
    ...(ownComment ? [ownComment] : []),
    ...new Map(
      (pages ?? [])
        .flatMap((page) => page.comments)
        .filter((comment) => comment.id !== ownComment?.id)
        .map((comment) => [comment.id, comment])
    ).values(),
  ];
  const scores = new Map(
    [...data.ratings, ...(data.own_rating ? [data.own_rating] : [])].map(
      (rating) => [rating.reviewer.id, rating.score]
    )
  );
  return (
    <Tabs
      value={view}
      onValueChange={setView}
      className="flex min-h-0 flex-1 flex-col gap-4 text-sm"
    >
      <div className="flex items-center justify-between gap-2 border-b">
        <TabsList
          aria-label="Task reviews"
          className="h-auto gap-3 rounded-none bg-transparent p-0"
        >
          <TabsTrigger value={reviewViews.write} className={reviewTab}>
            Your review
          </TabsTrigger>
          <TabsTrigger value={reviewViews.reviews} className={reviewTab}>
            Reviews ({data.comment_count})
          </TabsTrigger>
        </TabsList>
        {versionControl}
      </div>
      <TabsContent value={reviewViews.write} className="min-h-0 flex-1">
        <form
          className="flex h-full flex-col"
          onSubmit={(event) => {
            event.preventDefault();
            if (chosenScore === null || !body.trim()) return;
            const id = draft?.requestId ?? crypto.randomUUID();
            updateDraft({ requestId: id });
            void write(
              "",
              "POST",
              {
                score: chosenScore,
                expected_revision: data.own_rating_revision,
                body: body.trim(),
                client_request_id: id,
                ...(editing
                  ? {
                      comment_id: editing.id,
                      comment_revision: editing.revision,
                    }
                  : {}),
              },
              () => {
                onDraft();
                setView(reviewViews.reviews);
              }
            );
          }}
        >
          <fieldset
            disabled={busy}
            className="flex min-h-0 flex-1 flex-col gap-4"
          >
            <div className="flex flex-wrap items-center gap-3">
              <span id="review-rating-label">Rating</span>
              <div
                className="flex gap-2"
                role="group"
                aria-labelledby="review-rating-label"
              >
                {[1, 2, 3, 4, 5].map((value) => (
                  <Button
                    key={value}
                    type="button"
                    aria-label={`Rate ${value} out of 5`}
                    aria-pressed={chosenScore === value}
                    variant="outline"
                    className="aria-pressed:bg-primary aria-pressed:text-primary-foreground aria-pressed:hover:bg-primary w-9 px-0 tabular-nums"
                    size="sm"
                    onClick={() =>
                      updateDraft({ score: value, requestId: null })
                    }
                  >
                    {value}
                  </Button>
                ))}
              </div>
              {data.own_rating && chosenScore !== data.own_rating.score && (
                <span className="text-muted-foreground text-xs tabular-nums">
                  Previously {data.own_rating.score} / 5
                </span>
              )}
            </div>
            <div className="flex min-h-0 flex-1 flex-col gap-2">
              <label className="block" htmlFor="task-review-comment">
                Comment
              </label>
              <Textarea
                id="task-review-comment"
                className="min-h-0 flex-1 resize-none"
                required
                maxLength={10000}
                value={body}
                onChange={(event) => {
                  updateDraft({ body: event.target.value, requestId: null });
                }}
              />
            </div>
            <div className="flex items-center justify-end gap-3">
              {failure && (
                <p role="alert" className="mr-auto text-sm">
                  {failure}
                </p>
              )}
              <Button
                size="sm"
                disabled={chosenScore === null || !body.trim() || unchanged}
              >
                {editing ? "Save changes" : "Save review"}
              </Button>
            </div>
          </fieldset>
        </form>
      </TabsContent>
      <TabsContent
        value={reviewViews.reviews}
        className="flex min-h-0 flex-1 flex-col gap-3"
      >
        <div className="text-muted-foreground flex items-center justify-between gap-3">
          <span>
            Average{" "}
            <span className="text-foreground tabular-nums">
              {data.average_score == null
                ? "—"
                : `${data.average_score.toFixed(1)} / 5`}
            </span>
          </span>
        </div>
        {(failure || error) && <p role="alert">{failure || error?.message}</p>}
        <section
          className="min-h-0 flex-1 space-y-4 overflow-y-auto pr-2"
          aria-label="Review history"
        >
          {comments.length === 0 && data.ratings.length === 0 && (
            <p className="text-muted-foreground">No reviews yet.</p>
          )}
          {data.ratings
            .filter(
              (rating) =>
                !pages?.at(-1)?.next_before &&
                !comments.some(
                  (comment) => comment.reviewer.id === rating.reviewer.id
                )
            )
            .map((rating) => (
              <div
                className="flex items-center justify-between gap-2"
                key={rating.id}
              >
                <strong>
                  {rating.reviewer.name}
                  {rating.reviewer.id === data.current_user_id ? " (you)" : ""}
                </strong>
                <span className="font-mono tabular-nums">
                  {rating.score} / 5
                </span>
              </div>
            ))}
          {comments.map((comment) => (
            <article key={comment.id} className="space-y-1 border-b pb-3">
              <div className="flex items-center justify-between gap-2">
                <strong>
                  {comment.reviewer.name}
                  {comment.reviewer.id === data.current_user_id ? " (you)" : ""}
                </strong>
                {scores.has(comment.reviewer.id) && (
                  <span className="font-mono tabular-nums">
                    {scores.get(comment.reviewer.id)} / 5
                  </span>
                )}
              </div>
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
                  {comment.reviewer.id === data.current_user_id && (
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={busy}
                      onClick={() => {
                        updateDraft({
                          score: data.own_rating?.score ?? null,
                          editing: comment,
                          body: comment.body,
                          requestId: null,
                        });
                        setView(reviewViews.write);
                      }}
                    >
                      Edit
                    </Button>
                  )}
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={busy}
                    onClick={() => {
                      if (
                        window.confirm(
                          comment.reviewer.id === data.current_user_id
                            ? "Delete your review?"
                            : "Delete this comment?"
                        )
                      )
                        void write(
                          comment.reviewer.id === data.current_user_id
                            ? ""
                            : `/comments/${encodeURIComponent(comment.id)}`,
                          "DELETE",
                          {
                            expected_revision: comment.revision,
                            ...(comment.reviewer.id === data.current_user_id
                              ? {
                                  comment_id: comment.id,
                                  rating_revision: data.own_rating_revision,
                                }
                              : {}),
                          },
                          () => {
                            if (editing?.id === comment.id) onDraft();
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
              More reviews
            </Button>
          )}
        </section>
      </TabsContent>
    </Tabs>
  );
}
