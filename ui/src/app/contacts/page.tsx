import { Suspense } from "react";
import { ContactsPage } from "@/components/contacts/contacts-page";

export default function Contacts() {
  return (
    <Suspense fallback={null}>
      <ContactsPage />
    </Suspense>
  );
}
