"use client";

import { ArrowLeft, Contact as ContactIcon, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ContactList } from "@/components/contacts/contact-list";
import { ContactDetail } from "@/components/contacts/contact-detail";
import { ContactEditor } from "@/components/contacts/contact-editor";
import { useContactSelection } from "@/hooks/use-contacts";
import { useIsMobile } from "@/hooks/use-mobile";
import { useState } from "react";

export function ContactsPage() {
  const isMobile = useIsMobile();
  const { selectedId, selectContact } = useContactSelection();
  const [newOpen, setNewOpen] = useState(false);

  if (isMobile) {
    return (
      <div className="flex h-full flex-col overflow-hidden">
        {selectedId ? (
          <>
            <div className="flex items-center border-b px-2 py-1">
              <Button variant="ghost" size="sm" className="gap-1" onClick={() => selectContact(null)}>
                <ArrowLeft className="h-4 w-4" />
                Back
              </Button>
            </div>
            <ContactDetail contactId={selectedId} />
          </>
        ) : (
          <>
            <div className="flex items-center justify-end border-b px-2 py-1">
              <Button variant="ghost" size="sm" className="gap-1" onClick={() => setNewOpen(true)}>
                <Plus className="h-4 w-4" />
                New contact
              </Button>
            </div>
            <ContactList />
          </>
        )}
        <ContactEditor open={newOpen} onOpenChange={setNewOpen} />
      </div>
    );
  }

  return (
    <div className="flex h-full">
      <div className="flex h-full w-[360px] min-w-[280px] max-w-[480px] flex-shrink-0 flex-col overflow-hidden border-r">
        <div className="flex items-center justify-between border-b px-3 py-1.5">
          <span className="text-sm font-medium">Contacts</span>
          <Button variant="ghost" size="icon-sm" aria-label="New contact" onClick={() => setNewOpen(true)}>
            <Plus className="h-4 w-4" />
          </Button>
        </div>
        <ContactList />
      </div>
      <div className="flex h-full min-w-0 flex-1 flex-col overflow-hidden">
        {selectedId ? (
          <ContactDetail contactId={selectedId} />
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 text-muted-foreground">
            <ContactIcon className="h-16 w-16 opacity-30" />
            <p className="text-sm">Select a contact</p>
          </div>
        )}
      </div>
      <ContactEditor open={newOpen} onOpenChange={setNewOpen} />
    </div>
  );
}
