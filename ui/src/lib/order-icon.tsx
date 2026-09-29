import {
  Bus,
  Car,
  BedDouble,
  Download,
  Package,
  Plane,
  ReceiptText,
  Ticket,
  TrainFront,
  UtensilsCrossed,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import type { OrderIcon } from "@/types/api";

interface OrderIconEntry {
  Icon: LucideIcon;
  tileClass: string;
}

const ORDER_ICONS: Record<OrderIcon, OrderIconEntry> = {
  package: { Icon: Package, tileClass: "bg-amber-500/15 text-amber-700 dark:text-amber-300" },
  ticket: { Icon: Ticket, tileClass: "bg-violet-500/15 text-violet-700 dark:text-violet-300" },
  train: { Icon: TrainFront, tileClass: "bg-rose-500/15 text-rose-700 dark:text-rose-300" },
  plane: { Icon: Plane, tileClass: "bg-sky-500/15 text-sky-700 dark:text-sky-300" },
  bus: { Icon: Bus, tileClass: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300" },
  car: { Icon: Car, tileClass: "bg-teal-500/15 text-teal-700 dark:text-teal-300" },
  bed: { Icon: BedDouble, tileClass: "bg-indigo-500/15 text-indigo-700 dark:text-indigo-300" },
  food: {
    Icon: UtensilsCrossed,
    tileClass: "bg-orange-500/15 text-orange-700 dark:text-orange-300",
  },
  download: { Icon: Download, tileClass: "bg-cyan-500/15 text-cyan-700 dark:text-cyan-300" },
  wrench: { Icon: Wrench, tileClass: "bg-slate-500/15 text-slate-700 dark:text-slate-300" },
  receipt: { Icon: ReceiptText, tileClass: "bg-zinc-500/15 text-zinc-700 dark:text-zinc-300" },
};

/** Falls back to "receipt" for a value this build doesn't recognise -- a
 * server ahead of this client's own icon list is not an error state. */
export function orderIconEntry(icon: string): OrderIconEntry {
  return ORDER_ICONS[icon as OrderIcon] ?? ORDER_ICONS.receipt;
}
