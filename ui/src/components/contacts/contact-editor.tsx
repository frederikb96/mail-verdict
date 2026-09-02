"use client";

/** The contact edit form, in a Sheet. Sends the structured fields back;
 * the server rewrites the vCard `data`. */

import { useEffect, useState } from "react";
import { Loader2, Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Sheet,
  SheetContent,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { useAddressbooks, useCreateContact, useUpdateContact } from "@/hooks/use-contacts";
import { useToast } from "@/hooks/use-toast";
import type { Contact, ContactEmail } from "@/types/api";

interface ContactEditorProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  contact?: Contact;
}

export function ContactEditor({ open, onOpenChange, contact }: ContactEditorProps) {
  const { data: addressbooks } = useAddressbooks();
  const createContact = useCreateContact();
  const updateContact = useUpdateContact();
  const { push: pushToast } = useToast();
  const isEditing = !!contact;
  const writableAddressbooks = (addressbooks ?? []).filter((a) => !a.read_only);

  const [summary, setSummary] = useState(contact?.summary ?? "");
  const [emails, setEmails] = useState<ContactEmail[]>(
    contact?.emails ?? [{ email: "", type: null }],
  );
  const [organization, setOrganization] = useState(contact?.organization ?? "");
  const [title, setTitle] = useState(contact?.title ?? "");
  const [notes, setNotes] = useState(contact?.notes ?? "");
  const [addressbookId, setAddressbookId] = useState(writableAddressbooks[0]?.id ?? "");

  useEffect(() => {
    if (!open || isEditing) return;
    setAddressbookId((current) => current || writableAddressbooks[0]?.id || "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, isEditing, addressbooks]);

  const isPending = createContact.isPending || updateContact.isPending;

  const handleSave = () => {
    const cleanEmails = emails.filter((e) => e.email.trim());
    if (!summary.trim() || cleanEmails.length === 0) {
      pushToast("Add a name and at least one email", "warning");
      return;
    }
    if (isEditing) {
      updateContact.mutate(
        {
          id: contact.id,
          data: {
            summary,
            emails: cleanEmails,
            organization: organization || undefined,
            title: title || undefined,
            notes: notes || undefined,
          },
        },
        {
          onSuccess: () => onOpenChange(false),
          onError: (err) => pushToast(`Could not update contact: ${err.message}`, "error", 0),
        },
      );
    } else {
      createContact.mutate(
        {
          addressbook_id: addressbookId,
          summary,
          emails: cleanEmails,
          organization: organization || undefined,
          title: title || undefined,
          notes: notes || undefined,
        },
        {
          onSuccess: () => onOpenChange(false),
          onError: (err) => pushToast(`Could not create contact: ${err.message}`, "error", 0),
        },
      );
    }
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-md">
        <SheetHeader>
          <SheetTitle>{isEditing ? "Edit contact" : "New contact"}</SheetTitle>
        </SheetHeader>
        <div className="flex flex-col gap-3 overflow-y-auto px-4 pb-4">
          <div className="grid gap-1.5">
            <Label htmlFor="contact-name">Name</Label>
            <Input
              id="contact-name"
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              autoFocus
            />
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="contact-email-0">Email</Label>
            {emails.map((e, i) => (
              <div key={i} className="flex items-center gap-1">
                <Input
                  id={i === 0 ? "contact-email-0" : undefined}
                  type="email"
                  value={e.email}
                  onChange={(ev) =>
                    setEmails((prev) => prev.map((p, idx) => (idx === i ? { ...p, email: ev.target.value } : p)))
                  }
                />
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => setEmails((prev) => prev.filter((_, idx) => idx !== i))}
                >
                  <X className="h-3.5 w-3.5" />
                </Button>
              </div>
            ))}
            <Button
              variant="outline"
              size="sm"
              className="w-fit"
              onClick={() => setEmails((prev) => [...prev, { email: "", type: null }])}
            >
              <Plus className="mr-1 h-3.5 w-3.5" />
              Add email
            </Button>
          </div>

          <div className="grid gap-1.5">
            <Label>Organization</Label>
            <Input value={organization} onChange={(e) => setOrganization(e.target.value)} />
          </div>

          <div className="grid gap-1.5">
            <Label>Title</Label>
            <Input value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>

          {!isEditing && writableAddressbooks.length > 1 && (
            <div className="grid gap-1.5">
              <Label>Address book</Label>
              <select
                className="h-8 rounded-lg border border-input bg-transparent px-2 text-sm"
                value={addressbookId}
                onChange={(e) => setAddressbookId(e.target.value)}
              >
                {writableAddressbooks.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.display_name}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div className="grid gap-1.5">
            <Label>Notes</Label>
            <Textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={3} />
          </div>
        </div>
        <SheetFooter className="flex-row justify-end gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={isPending}>
            {isPending && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}
            Save
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}
