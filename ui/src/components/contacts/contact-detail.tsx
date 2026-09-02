"use client";

import { useState } from "react";
import { useSetAtom } from "jotai";
import {
  Building2,
  Cake,
  Link2,
  Loader2,
  Mail,
  MapPin,
  Pencil,
  Phone,
  StickyNote,
  Trash2,
  UserRound,
} from "lucide-react";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ContactEditor } from "@/components/contacts/contact-editor";
import { useContact, useDeleteContact } from "@/hooks/use-contacts";
import { composeIntentAtom, selectedContactIdAtom } from "@/lib/atoms";
import { getInitials } from "@/lib/format";
import { format } from "@/lib/dates";

export function ContactDetail({ contactId }: { contactId: string }) {
  const { data: contact, isLoading } = useContact(contactId);
  const deleteContact = useDeleteContact();
  const setComposeIntent = useSetAtom(composeIntentAtom);
  const setSelectedId = useSetAtom(selectedContactIdAtom);
  const [editorOpen, setEditorOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  if (isLoading || !contact) {
    return (
      <div className="flex h-full items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <div className="flex items-start justify-between gap-3 border-b p-4">
        <div className="flex items-center gap-3">
          <Avatar size="lg">
            <AvatarFallback>{getInitials(contact.summary)}</AvatarFallback>
          </Avatar>
          <div>
            <h2 className="text-lg font-semibold">{contact.summary}</h2>
            {contact.title && contact.organization && (
              <p className="text-sm text-muted-foreground">
                {contact.title} at {contact.organization}
              </p>
            )}
            {!contact.title && contact.organization && (
              <p className="text-sm text-muted-foreground">{contact.organization}</p>
            )}
          </div>
        </div>
        {!contact.read_only && (
          <div className="flex shrink-0 gap-1">
            <Button variant="ghost" size="icon-sm" onClick={() => setEditorOpen(true)}>
              <Pencil className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              className="text-destructive"
              onClick={() => setConfirmDelete(true)}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
        )}
      </div>

      <div className="flex flex-col gap-4 p-4">
        {contact.emails.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              <Mail className="h-3.5 w-3.5" />
              Email
            </span>
            {contact.emails.map((e) => (
              <div key={e.email} className="flex items-center justify-between text-sm">
                <span>{e.email}</span>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-xs"
                  onClick={() => setComposeIntent({ to: [e.email] })}
                >
                  Compose
                </Button>
              </div>
            ))}
          </div>
        )}

        {contact.phones.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              <Phone className="h-3.5 w-3.5" />
              Phone
            </span>
            {contact.phones.map((p, i) => (
              <span key={i} className="text-sm">
                {p.number}
              </span>
            ))}
          </div>
        )}

        {contact.addresses.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              <MapPin className="h-3.5 w-3.5" />
              Address
            </span>
            {contact.addresses.map((a, i) => (
              <span key={i} className="whitespace-pre-line text-sm">
                {a.text}
              </span>
            ))}
          </div>
        )}

        {contact.birthday && (
          <div className="flex items-center gap-1.5 text-sm">
            <Cake className="h-3.5 w-3.5 text-muted-foreground" />
            {format(new Date(contact.birthday), "MMMM d, yyyy")}
          </div>
        )}

        {contact.url && (
          <div className="flex items-center gap-1.5 text-sm">
            <Link2 className="h-3.5 w-3.5 text-muted-foreground" />
            <a href={contact.url} target="_blank" rel="noreferrer" className="underline">
              {contact.url}
            </a>
          </div>
        )}

        {contact.organization && !contact.title && (
          <div className="flex items-center gap-1.5 text-sm">
            <Building2 className="h-3.5 w-3.5 text-muted-foreground" />
            {contact.organization}
          </div>
        )}

        {contact.notes && (
          <div className="flex flex-col gap-1.5">
            <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              <StickyNote className="h-3.5 w-3.5" />
              Notes
            </span>
            <p className="whitespace-pre-line text-sm">{contact.notes}</p>
          </div>
        )}

        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <UserRound className="h-3.5 w-3.5" />
          {contact.addressbook_name}
        </div>
      </div>

      <ContactEditor open={editorOpen} onOpenChange={setEditorOpen} contact={contact} />

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`Delete "${contact.summary}"?`}
        description="This removes the contact from the address book. It cannot be undone."
        isConfirming={deleteContact.isPending}
        onConfirm={() =>
          deleteContact.mutate(contact.id, {
            onSuccess: () => {
              setConfirmDelete(false);
              setSelectedId(null);
            },
          })
        }
      />
    </div>
  );
}
