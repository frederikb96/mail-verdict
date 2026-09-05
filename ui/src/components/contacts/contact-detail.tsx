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
  Tag,
  Trash2,
  UserRound,
} from "lucide-react";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ContactEditor } from "@/components/contacts/contact-editor";
import { useContact, useContactSelection, useDeleteContact } from "@/hooks/use-contacts";
import { composeIntentAtom } from "@/lib/atoms";
import { formatContactBirthday, getInitials } from "@/lib/format";

export function ContactDetail({ contactId }: { contactId: string }) {
  const { data: contact, isLoading } = useContact(contactId);
  const deleteContact = useDeleteContact();
  const setComposeIntent = useSetAtom(composeIntentAtom);
  const { selectContact } = useContactSelection();
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
            {contact.photo?.kind === "embedded" && <AvatarImage src={contact.photo.url} />}
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
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Edit contact"
              onClick={() => setEditorOpen(true)}
            >
              <Pencil className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              className="text-destructive"
              aria-label="Delete contact"
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
                {p.type && <span className="ml-1.5 text-xs text-muted-foreground">{p.type}</span>}
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
                {a.label && <span className="ml-1.5 text-xs text-muted-foreground">{a.label}</span>}
              </span>
            ))}
          </div>
        )}

        {contact.birthday && (
          <div className="flex items-center gap-1.5 text-sm">
            <Cake className="h-3.5 w-3.5 text-muted-foreground" />
            {formatContactBirthday(contact.birthday) ?? (
              // A real but unparseable value (e.g. a calendar date that
              // doesn't exist, such as a stray Feb 29 in a non-leap year)
              // -- shown rather than silently vanishing, so its presence
              // on the card is not lost from view entirely.
              <span className="italic text-muted-foreground">
                Birthday could not be read
              </span>
            )}
          </div>
        )}

        {contact.urls.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              <Link2 className="h-3.5 w-3.5" />
              Website
            </span>
            {contact.urls.map((u) => (
              <a key={u} href={u} target="_blank" rel="noreferrer" className="text-sm underline">
                {u}
              </a>
            ))}
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

        {contact.categories.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              <Tag className="h-3.5 w-3.5" />
              Categories
            </span>
            <div className="flex flex-wrap gap-1">
              {contact.categories.map((c) => (
                <Badge key={c} variant="outline">
                  {c}
                </Badge>
              ))}
            </div>
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
              selectContact(null);
            },
          })
        }
      />
    </div>
  );
}
