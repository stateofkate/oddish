"use client";

import { useState } from "react";
import useSWR from "swr";
import { Check, ChevronDown, Users, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { fetcher } from "@/lib/api";
import { cn } from "@/lib/utils";

type PeopleSearchItem = {
  id: string;
  email: string;
  github_username: string | null;
};

export function MemberFilterTypeahead({
  selectedUserId,
  onSelect,
}: {
  selectedUserId: string | null;
  onSelect: (userId: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const { data, error, isLoading, mutate } = useSWR<{
    items: PeopleSearchItem[];
  }>("/api/people/search?all_members=true", fetcher, {
    revalidateOnFocus: false,
    revalidateOnReconnect: false,
    shouldRetryOnError: false,
  });
  const selectedPerson = data?.items.find(
    (person) => person.id === selectedUserId
  );
  const search = query.trim().toLowerCase();
  const people = (data?.items ?? []).filter(
    (person) =>
      person.email.toLowerCase().includes(search) ||
      (person.github_username != null &&
        person.github_username.toLowerCase().includes(search.replace(/^@/, "")))
  );

  return (
    <div className="flex w-full items-center gap-1 sm:w-auto">
      <Popover
        open={open}
        onOpenChange={(nextOpen) => {
          setOpen(nextOpen);
          if (!nextOpen) setQuery("");
        }}
      >
        <PopoverTrigger asChild>
          <Button
            type="button"
            variant={selectedUserId ? "secondary" : "outline"}
            size="sm"
            className="h-8 w-full justify-between border-[#6f88b4]/20 sm:w-[180px]"
            aria-expanded={open}
            aria-label="Filter experiments by member"
          >
            <span className="flex min-w-0 items-center gap-1.5">
              <Users className="h-3.5 w-3.5 shrink-0 opacity-60" />
              <span className="truncate">
                {selectedPerson?.email ?? "Members"}
              </span>
            </span>
            <ChevronDown className="h-4 w-4 opacity-50" />
          </Button>
        </PopoverTrigger>
        <PopoverContent align="end" className="w-[260px] p-0">
          <div className="border-b p-2">
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search members…"
              aria-label="Search members"
            />
          </div>
          <div
            className="max-h-[300px] overflow-y-auto p-1"
            aria-label="Member results"
          >
            {isLoading ? (
              <p className="text-muted-foreground p-4 text-center text-sm">
                Loading members…
              </p>
            ) : error ? (
              <div role="alert" className="p-2 text-center text-sm">
                Could not load members.
                <Button variant="ghost" size="sm" onClick={() => void mutate()}>
                  Retry
                </Button>
              </div>
            ) : people.length === 0 ? (
              <p className="text-muted-foreground p-4 text-center text-sm">
                No members found.
              </p>
            ) : (
              people.map((person) => (
                <button
                  key={person.id}
                  type="button"
                  aria-pressed={selectedUserId === person.id}
                  className={cn(
                    "hover:bg-accent focus-visible:bg-accent flex w-full items-center rounded-sm px-2 py-1.5 text-left text-sm focus-visible:outline-none",
                    selectedUserId === person.id && "bg-accent"
                  )}
                  onClick={() => {
                    onSelect(selectedUserId === person.id ? null : person.id);
                    setOpen(false);
                    setQuery("");
                  }}
                >
                  <Check
                    className={cn(
                      "mr-2 h-4 w-4 shrink-0",
                      selectedUserId === person.id ? "opacity-100" : "opacity-0"
                    )}
                  />
                  <span className="flex min-w-0 flex-col">
                    <span className="truncate">{person.email}</span>
                    {person.github_username && (
                      <span className="text-muted-foreground truncate text-[10px]">
                        @{person.github_username}
                      </span>
                    )}
                  </span>
                </button>
              ))
            )}
          </div>
        </PopoverContent>
      </Popover>
      {selectedUserId && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-8 w-8 shrink-0 p-0"
          aria-label="Clear member filter"
          onClick={() => onSelect(null)}
        >
          <X className="h-4 w-4" />
        </Button>
      )}
    </div>
  );
}
