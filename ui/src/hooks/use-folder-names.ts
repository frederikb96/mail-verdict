/** Folder id to display name, across the caller's accounts. */

import { useCallback, useMemo } from "react";
import { useSearchFolders } from "@/hooks/use-search-folders";
import { folderDisplayName } from "@/lib/folders";

export function useFolderNames(): (folderId: string) => string | undefined {
  const { options } = useSearchFolders();
  const names = useMemo(
    () => new Map(options.map((o) => [o.folder.id, folderDisplayName(o.folder)])),
    [options],
  );
  return useCallback((folderId: string) => names.get(folderId), [names]);
}
