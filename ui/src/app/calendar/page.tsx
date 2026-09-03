import { CalendarPage } from "@/components/calendar/calendar-page";
import { ClientOnly } from "@/components/client-only";

export default function Calendar() {
  return (
    <ClientOnly>
      <CalendarPage />
    </ClientOnly>
  );
}
