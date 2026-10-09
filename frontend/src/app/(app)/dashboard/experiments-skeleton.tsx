import type { ReactNode } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

// Reuse the live headers so changing a filter doesn't switch column layouts.
export function ExperimentsSkeleton({
  rows = 10,
  headers,
  columns,
}: {
  rows?: number;
  headers: ReactNode;
  columns: number;
}) {
  return (
    <div
      className="min-h-[560px] overflow-x-hidden"
      aria-busy="true"
      aria-label="Loading experiments"
    >
      <Table>
        <TableHeader>
          <TableRow>{headers}</TableRow>
        </TableHeader>
        <TableBody>
          {Array.from({ length: rows }, (_, row) => (
            <TableRow key={row}>
              {Array.from({ length: columns }, (_, column) => (
                <TableCell key={column}>
                  <Skeleton
                    className={column === 0 ? "h-3.5 w-40" : "h-3.5 w-16"}
                  />
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
