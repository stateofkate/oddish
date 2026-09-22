"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import useSWRInfinite from "swr/infinite";
import { Check, ChevronDown, Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { fetcher } from "@/lib/api";
import { cn } from "@/lib/utils";

type PeopleSearchItem = {
  id: string;
  email: string;
  github_username: string | null;
};

type PeopleSearchResponse = {
  items: PeopleSearchItem[];
  next_offset: number | null;
};

export function MemberFilterTypeahead({
  selectedUserId,
  onSelect,
}: {
  selectedUserId: string | null;
  onSelect: (userId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");

  useEffect(() => {
    const timeoutId = window.setTimeout(
      () => setDebouncedQuery(query.trim()),
      250
    );
    return () => window.clearTimeout(timeoutId);
  }, [query]);

  const selectedLookupPath = selectedUserId
    ? `/api/people/search?q=${encodeURIComponent(selectedUserId)}&limit=1`
    : null;
  const { data: selectedResponse } = useSWR<PeopleSearchResponse>(
    selectedLookupPath,
    fetcher,
    { revalidateOnFocus: false, shouldRetryOnError: false }
  );
  const {
    data: pages,
    error,
    isLoading,
    isValidating,
    size,
    setSize,
    mutate,
  } = useSWRInfinite<PeopleSearchResponse>(
    (index, previousPage: PeopleSearchResponse | null) => {
      if (!open || (previousPage && previousPage.next_offset == null))
        return null;
      const offset = index === 0 ? 0 : previousPage!.next_offset;
      return `/api/people/search?q=${encodeURIComponent(debouncedQuery)}&limit=25&offset=${offset}`;
    },
    fetcher,
    {
      revalidateOnFocus: false,
      shouldRetryOnError: false,
      revalidateFirstPage: false,
    }
  );

  const selectedPerson = selectedResponse?.items.find(
    (person) => person.id === selectedUserId
  );
  const queryIsSettled = query.trim() === debouncedQuery;
  const people = queryIsSettled
    ? (pages?.flatMap((page) => page.items) ?? [])
    : [];
  const isSearching = !queryIsSettled || isLoading;
  const hasMore = queryIsSettled && pages?.at(-1)?.next_offset != null;
  const isLoadingMore =
    isValidating || (pages !== undefined && size > pages.length && !error);

  return (
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
          role="combobox"
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
        <Command shouldFilter={false}>
          <CommandInput
            value={query}
            onValueChange={setQuery}
            placeholder="Search members…"
          />
          <CommandList
            key={debouncedQuery}
            onScroll={(event) => {
              const list = event.currentTarget;
              if (
                hasMore &&
                !isLoadingMore &&
                !error &&
                list.scrollHeight - list.scrollTop - list.clientHeight < 48
              ) {
                void setSize(size + 1);
              }
            }}
          >
            {isSearching ? (
              <div className="text-muted-foreground py-6 text-center text-sm">
                Searching…
              </div>
            ) : (
              !error && <CommandEmpty>No members found.</CommandEmpty>
            )}
            <CommandGroup>
              {people.map((person) => (
                <CommandItem
                  key={person.id}
                  value={person.id}
                  onSelect={() => {
                    onSelect(person.id);
                    setOpen(false);
                    setQuery("");
                  }}
                >
                  <Check
                    className={cn(
                      "mr-2 h-4 w-4",
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
                </CommandItem>
              ))}
            </CommandGroup>
            {error ? (
              <div role="alert" className="p-2 text-center text-sm">
                Could not load members.
                <Button variant="ghost" size="sm" onClick={() => void mutate()}>
                  Retry
                </Button>
              </div>
            ) : (
              hasMore && (
                <Button
                  variant="ghost"
                  className="w-full"
                  disabled={isLoadingMore}
                  onClick={() => void setSize(size + 1)}
                >
                  {isLoadingMore ? "Loading more…" : "Load more members"}
                </Button>
              )
            )}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
