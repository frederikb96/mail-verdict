"use client";

import { CategoryHeader } from "@/components/settings/blocks";
import { SecretsCard } from "@/components/settings/secrets-card";

export function SecretsSettings() {
  return (
    <>
      <CategoryHeader
        title="Secrets"
        description="Named values a rule's webhook action can put into a header, so a token never sits in a rule itself."
      />
      <SecretsCard />
    </>
  );
}
