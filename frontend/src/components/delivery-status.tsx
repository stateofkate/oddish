import {
  AlertCircle,
  CheckCircle2,
  CircleDashed,
  Clock,
  Loader2,
  XCircle,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { qaStatusLabel } from "@/lib/deliveries";
import type { DeliveryQAStatus } from "@/lib/types";
import { Badge } from "@/components/ui/badge";

const QA_PRESENTATION = {
  accepted: {
    Icon: CheckCircle2,
    tone: "text-emerald-700 dark:text-emerald-400",
  },
  needs_fixes: { Icon: XCircle, tone: "text-red-700 dark:text-red-400" },
  error: { Icon: AlertCircle, tone: "text-amber-700 dark:text-amber-400" },
  // Same label as never: this version's trials have no current verdict.
  outdated: { Icon: CircleDashed, tone: "text-muted-foreground" },
  running: { Icon: Loader2, tone: "text-blue-700 dark:text-blue-400" },
  queued: { Icon: Clock, tone: "text-blue-700 dark:text-blue-400" },
  never: { Icon: CircleDashed, tone: "text-muted-foreground" },
};

export function DeliveryQAStatusBadge({ qa }: { qa: DeliveryQAStatus }) {
  const { Icon, tone } = QA_PRESENTATION[qa.status];
  return (
    <span
      title={qa.detail}
      className={cn("inline-flex items-center gap-1 text-sm", tone)}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      {qaStatusLabel(qa.status)}
    </span>
  );
}

export function DeliveryStatusBadge({ status }: { status: string }) {
  if (status === "finalized") {
    return (
      <Badge className="bg-emerald-500/15 text-emerald-700 hover:bg-emerald-500/15 dark:text-emerald-400">
        Finalized
      </Badge>
    );
  }
  return <Badge variant="secondary">Active</Badge>;
}
