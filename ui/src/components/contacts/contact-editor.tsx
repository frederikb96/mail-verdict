"use client";

/** The contact edit form, in a Sheet. Sends the structured fields back;
 * the server rewrites the vCard `data`. */

import { useEffect, useRef, useState } from "react";
import { Loader2, Plus, Upload, X } from "lucide-react";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
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
import { getInitials, parseContactBirthday } from "@/lib/format";
import type { Contact, ContactEmail } from "@/types/api";

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/** Reads a chosen file as a `data:` URL -- what the create/update request's
 * `photo_data_url` expects, and what the vCard PHOTO property is built
 * from server-side. */
function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

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

  const [phones, setPhones] = useState(
    contact?.phones.map((p) => ({ number: p.number, type: p.type ?? "" })) ?? [],
  );
  const [addresses, setAddresses] = useState(
    contact?.addresses.map((a) => ({ label: a.label ?? "", text: a.text })) ?? [],
  );
  const [urls, setUrls] = useState<string[]>(contact?.urls ?? []);
  const [categoriesInput, setCategoriesInput] = useState(contact?.categories.join(", ") ?? "");

  const initialBirthday = contact?.birthday ? parseContactBirthday(contact.birthday) : null;
  const [birthdayHasYear, setBirthdayHasYear] = useState(initialBirthday?.year !== null);
  const [birthdayDate, setBirthdayDate] = useState(
    initialBirthday?.year != null
      ? `${String(initialBirthday.year).padStart(4, "0")}-`
        + `${String(initialBirthday.month).padStart(2, "0")}-`
        + `${String(initialBirthday.day).padStart(2, "0")}`
      : "",
  );
  const [birthdayMonth, setBirthdayMonth] = useState(
    initialBirthday && initialBirthday.year === null ? String(initialBirthday.month) : "",
  );
  const [birthdayDay, setBirthdayDay] = useState(
    initialBirthday && initialBirthday.year === null ? String(initialBirthday.day) : "",
  );

  // undefined = untouched (an existing photo, if any, is left alone on
  // update); null = removed; a data: URI = a freshly chosen file.
  const [photoDataUrl, setPhotoDataUrl] = useState<string | null | undefined>(undefined);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const existingPhotoUrl = contact?.photo?.kind === "embedded" ? contact.photo.url : null;
  const photoPreview = photoDataUrl !== undefined ? photoDataUrl : existingPhotoUrl;

  useEffect(() => {
    if (!open || isEditing) return;
    setAddressbookId((current) => current || writableAddressbooks[0]?.id || "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, isEditing, addressbooks]);

  const isPending = createContact.isPending || updateContact.isPending;

  const handlePhotoChosen = async (file: File) => {
    try {
      setPhotoDataUrl(await fileToDataUrl(file));
    } catch {
      pushToast("Could not read that image", "error");
    }
  };

  const handleSave = () => {
    const cleanEmails = emails.filter((e) => e.email.trim());
    if (!summary.trim() || cleanEmails.length === 0) {
      pushToast("Add a name and at least one email", "warning");
      return;
    }
    const cleanPhones = phones
      .filter((p) => p.number.trim())
      .map((p) => ({ number: p.number.trim(), type: p.type.trim() || undefined }));
    const cleanAddresses = addresses
      .filter((a) => a.text.trim())
      .map((a) => ({ label: a.label.trim() || undefined, text: a.text.trim() }));
    const cleanUrls = urls.map((u) => u.trim()).filter(Boolean);
    const categories = categoriesInput.split(",").map((c) => c.trim()).filter(Boolean);
    const birthday = birthdayHasYear
      ? birthdayDate || undefined
      : birthdayMonth && birthdayDay
        ? `--${birthdayMonth.padStart(2, "0")}-${birthdayDay.padStart(2, "0")}`
        : undefined;

    if (isEditing) {
      updateContact.mutate(
        {
          id: contact.id,
          data: {
            summary,
            emails: cleanEmails,
            organization: organization || undefined,
            title: title || undefined,
            phones: cleanPhones,
            addresses: cleanAddresses,
            birthday,
            urls: cleanUrls,
            notes: notes || undefined,
            categories,
            // Untouched (undefined) leaves the stored photo as it is --
            // only a deliberate upload or removal sends this field.
            photo_data_url: photoDataUrl !== undefined ? (photoDataUrl ?? "") : undefined,
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
          phones: cleanPhones,
          addresses: cleanAddresses,
          birthday,
          urls: cleanUrls,
          notes: notes || undefined,
          categories,
          photo_data_url: photoDataUrl ?? undefined,
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
          <div className="flex items-center gap-3">
            <Avatar size="lg">
              {photoPreview && <AvatarImage src={photoPreview} />}
              <AvatarFallback>{getInitials(summary || "?")}</AvatarFallback>
            </Avatar>
            <div className="flex flex-col gap-1">
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) void handlePhotoChosen(file);
                  e.target.value = "";
                }}
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="w-fit"
                onClick={() => fileInputRef.current?.click()}
              >
                <Upload className="mr-1 h-3.5 w-3.5" />
                {photoPreview ? "Change photo" : "Add photo"}
              </Button>
              {photoPreview && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="w-fit text-muted-foreground"
                  onClick={() => setPhotoDataUrl(null)}
                >
                  Remove photo
                </Button>
              )}
              {!photoPreview && contact?.photo?.kind === "url" && (
                <p className="text-xs text-muted-foreground">
                  Has a photo hosted elsewhere -- not shown here.
                </p>
              )}
            </div>
          </div>

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
            <Label htmlFor="contact-organization">Organization</Label>
            <Input
              id="contact-organization"
              value={organization}
              onChange={(e) => setOrganization(e.target.value)}
            />
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="contact-title">Title</Label>
            <Input id="contact-title" value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>

          <div className="grid gap-1.5">
            <Label>Phone</Label>
            {phones.map((p, i) => (
              <div key={i} className="flex items-center gap-1">
                <Input
                  value={p.number}
                  placeholder="Number"
                  className="flex-[2]"
                  onChange={(ev) =>
                    setPhones((prev) =>
                      prev.map((row, idx) => (idx === i ? { ...row, number: ev.target.value } : row)),
                    )
                  }
                />
                <Input
                  value={p.type}
                  placeholder="Type"
                  className="flex-1"
                  onChange={(ev) =>
                    setPhones((prev) =>
                      prev.map((row, idx) => (idx === i ? { ...row, type: ev.target.value } : row)),
                    )
                  }
                />
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => setPhones((prev) => prev.filter((_, idx) => idx !== i))}
                >
                  <X className="h-3.5 w-3.5" />
                </Button>
              </div>
            ))}
            <Button
              variant="outline"
              size="sm"
              className="w-fit"
              onClick={() => setPhones((prev) => [...prev, { number: "", type: "" }])}
            >
              <Plus className="mr-1 h-3.5 w-3.5" />
              Add phone
            </Button>
          </div>

          <div className="grid gap-1.5">
            <Label>Address</Label>
            {addresses.map((a, i) => (
              <div key={i} className="flex flex-col gap-1 rounded-md border p-2">
                <div className="flex items-center gap-1">
                  <Input
                    value={a.label}
                    placeholder="Type (e.g. home, work)"
                    onChange={(ev) =>
                      setAddresses((prev) =>
                        prev.map((row, idx) => (idx === i ? { ...row, label: ev.target.value } : row)),
                      )
                    }
                  />
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    onClick={() => setAddresses((prev) => prev.filter((_, idx) => idx !== i))}
                  >
                    <X className="h-3.5 w-3.5" />
                  </Button>
                </div>
                <Textarea
                  value={a.text}
                  rows={2}
                  placeholder="Street, city, region, postal code, country"
                  onChange={(ev) =>
                    setAddresses((prev) =>
                      prev.map((row, idx) => (idx === i ? { ...row, text: ev.target.value } : row)),
                    )
                  }
                />
              </div>
            ))}
            <Button
              variant="outline"
              size="sm"
              className="w-fit"
              onClick={() => setAddresses((prev) => [...prev, { label: "", text: "" }])}
            >
              <Plus className="mr-1 h-3.5 w-3.5" />
              Add address
            </Button>
          </div>

          <div className="grid gap-1.5">
            <Label>Birthday</Label>
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <input
                type="checkbox"
                checked={!birthdayHasYear}
                onChange={(e) => setBirthdayHasYear(!e.target.checked)}
              />
              I don't know the year
            </label>
            {birthdayHasYear ? (
              <Input
                type="date"
                value={birthdayDate}
                onChange={(e) => setBirthdayDate(e.target.value)}
              />
            ) : (
              <div className="flex items-center gap-1">
                <select
                  className="h-8 flex-1 rounded-lg border border-input bg-transparent px-2 text-sm"
                  value={birthdayMonth}
                  onChange={(e) => setBirthdayMonth(e.target.value)}
                >
                  <option value="">Month</option>
                  {MONTH_NAMES.map((name, idx) => (
                    <option key={name} value={String(idx + 1)}>
                      {name}
                    </option>
                  ))}
                </select>
                <select
                  className="h-8 w-20 rounded-lg border border-input bg-transparent px-2 text-sm"
                  value={birthdayDay}
                  onChange={(e) => setBirthdayDay(e.target.value)}
                >
                  <option value="">Day</option>
                  {Array.from({ length: 31 }, (_, idx) => idx + 1).map((day) => (
                    <option key={day} value={String(day)}>
                      {day}
                    </option>
                  ))}
                </select>
              </div>
            )}
          </div>

          <div className="grid gap-1.5">
            <Label>Website</Label>
            {urls.map((u, i) => (
              <div key={i} className="flex items-center gap-1">
                <Input
                  value={u}
                  placeholder="https://"
                  onChange={(ev) =>
                    setUrls((prev) => prev.map((row, idx) => (idx === i ? ev.target.value : row)))
                  }
                />
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => setUrls((prev) => prev.filter((_, idx) => idx !== i))}
                >
                  <X className="h-3.5 w-3.5" />
                </Button>
              </div>
            ))}
            <Button
              variant="outline"
              size="sm"
              className="w-fit"
              onClick={() => setUrls((prev) => [...prev, ""])}
            >
              <Plus className="mr-1 h-3.5 w-3.5" />
              Add website
            </Button>
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
            <Label htmlFor="contact-notes">Notes</Label>
            <Textarea
              id="contact-notes"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={3}
            />
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="contact-categories">Categories</Label>
            <Input
              id="contact-categories"
              value={categoriesInput}
              placeholder="Friend, Work"
              onChange={(e) => setCategoriesInput(e.target.value)}
            />
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
