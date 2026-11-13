"use client";

import { AccountsPage } from "@/components/accounts/accounts-page";
import { AccountOrder } from "@/components/settings/account-order";

export function AccountsSettings() {
  return (
    <>
      <AccountsPage />
      <AccountOrder />
    </>
  );
}
