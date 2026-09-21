"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@clerk/nextjs";
import useSWR from "swr";
import { Package, Plus } from "lucide-react";

import { fetcher } from "@/lib/api";
import { isOrgAdminRole } from "@/lib/org-roles";
import type { DeliveryListItem } from "@/lib/types";
import { DeliveryCreateDialog } from "@/components/delivery-create-dialog";
import { DeliveryStatusBadge } from "@/components/delivery-status";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export function DeliveriesClient({
  initialDeliveries,
}: {
  initialDeliveries: DeliveryListItem[] | null;
}) {
  const router = useRouter();
  const { orgRole } = useAuth();
  const isAdmin = isOrgAdminRole(orgRole);
  const { data, error, isLoading, mutate } = useSWR<DeliveryListItem[]>(
    "/api/deliveries",
    fetcher,
    { fallbackData: initialDeliveries ?? undefined }
  );

  const [createOpen, setCreateOpen] = useState(false);

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-2">
        <CardTitle className="flex items-center gap-2">
          <Package className="h-5 w-5" />
          Deliveries
        </CardTitle>
        {isAdmin && (
          <>
            <Button size="sm" onClick={() => setCreateOpen(true)}><Plus className="mr-1 h-4 w-4" />New delivery</Button>
            {createOpen ? <DeliveryCreateDialog onClose={() => setCreateOpen(false)} onCreated={delivery => {
              setCreateOpen(false);
              void mutate();
              router.push(`/deliveries/${delivery.id}`);
            }} /> : null}
          </>
        )}
      </CardHeader>
      <CardContent>
        {error ? (
          <p className="text-destructive text-sm">
            Failed to load deliveries: {error.message}
          </p>
        ) : isLoading && !data ? (
          <div className="space-y-2">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-2/3" />
          </div>
        ) : !data || data.length === 0 ? (
          <p className="text-muted-foreground text-sm">
            No deliveries yet. A delivery is a checklist that tracks whether a
            set of tasks is ready to ship to a customer.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Customer</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Tasks</TableHead>
                <TableHead>Created</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.map((delivery) => (
                <TableRow key={delivery.id}>
                  <TableCell>
                    <Link
                      href={`/deliveries/${delivery.id}`}
                      className="font-medium hover:underline"
                    >
                      {delivery.name}
                    </Link>
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {delivery.customer_name || "—"}
                  </TableCell>
                  <TableCell>
                    <DeliveryStatusBadge status={delivery.status} />
                  </TableCell>
                  <TableCell className="text-right">
                    {delivery.task_count}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {new Date(delivery.created_at).toLocaleDateString()}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
