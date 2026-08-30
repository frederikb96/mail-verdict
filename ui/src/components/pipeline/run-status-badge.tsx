import { Badge } from "@/components/ui/badge";

const STATUS_VARIANT: Record<string, "secondary" | "destructive" | "outline"> = {
  done: "secondary",
  failed: "destructive",
  skipped: "outline",
  pending: "outline",
  claimed: "outline",
  cancelled: "outline",
};

export function RunStatusBadge({ status }: { status: string }) {
  return <Badge variant={STATUS_VARIANT[status] ?? "outline"}>{status}</Badge>;
}
