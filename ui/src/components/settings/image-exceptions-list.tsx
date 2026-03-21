"use client";

import { Trash2, ImageOff, Mail, Globe } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  useImageExceptions,
  useDeleteImageException,
} from "@/hooks/use-image-exceptions";

interface ImageExceptionsListProps {
  accountId: string | null;
}

/**
 * Settings page section listing all image loading exceptions.
 * Users can delete exceptions but cannot add from here (adding is per-message).
 */
export function ImageExceptionsList({ accountId }: ImageExceptionsListProps) {
  const { data: exceptions, isLoading } = useImageExceptions(accountId);
  const deleteException = useDeleteImageException();

  if (!accountId) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <ImageOff className="h-4 w-4" />
            Image Exceptions
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Select an account to manage image exceptions
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <ImageOff className="h-4 w-4" />
          Image Exceptions
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="mb-3 text-xs text-muted-foreground">
          Senders and domains allowed to load remote images. Add exceptions from
          the mail reading view.
        </p>

        {isLoading && (
          <div className="py-4 text-sm text-muted-foreground">Loading...</div>
        )}

        {!isLoading && (!exceptions || exceptions.length === 0) && (
          <div className="py-4 text-sm text-muted-foreground">
            No image exceptions configured
          </div>
        )}

        {exceptions && exceptions.length > 0 && (
          <div className="divide-y rounded-md border">
            {exceptions.map((exc) => (
              <div
                key={exc.id}
                className="flex items-center justify-between px-3 py-2"
              >
                <div className="flex items-center gap-2">
                  {exc.type === "sender" ? (
                    <Mail className="h-3.5 w-3.5 text-muted-foreground" />
                  ) : (
                    <Globe className="h-3.5 w-3.5 text-muted-foreground" />
                  )}
                  <span className="text-sm">{exc.value}</span>
                  <span className="text-xs text-muted-foreground">
                    ({exc.type})
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-muted-foreground">
                    {new Date(exc.created_at).toLocaleDateString()}
                  </span>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    disabled={deleteException.isPending}
                    onClick={() =>
                      deleteException.mutate({
                        accountId,
                        exceptionId: exc.id,
                      })
                    }
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
