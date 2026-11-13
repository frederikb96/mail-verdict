/** Which settings keys belong to users and which to administrators. */

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function useSettingsSchema() {
  return useQuery({
    queryKey: ["settings-schema"],
    queryFn: () => api.settings.schema(),
    staleTime: Infinity,
    // A server that predates the schema answers 404; the forms that consult
    // it then show their declared fields only.
    retry: false,
  });
}
