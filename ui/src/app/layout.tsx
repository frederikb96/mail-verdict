import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "@/components/providers";
import { ThemeProvider } from "@/components/theme-provider";
import { SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";
import { AppSidebar } from "@/components/layout/app-sidebar";
import { SSEConnector } from "@/components/layout/sse-connector";
import { MailDndProvider } from "@/components/mail/dnd-provider";
import { ConnectionIndicator } from "@/components/layout/connection-indicator";
import { ErrorBoundary } from "@/components/error/error-boundary";

export const metadata: Metadata = {
  title: "MailVerdict",
  description: "AI-powered email management",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="h-full antialiased" suppressHydrationWarning>
      <body className="flex h-full flex-col">
        <Providers>
          <ThemeProvider>
            <SidebarProvider>
              <SSEConnector />
              <MailDndProvider>
                <ErrorBoundary section="sidebar">
                  <AppSidebar />
                </ErrorBoundary>
                <main className="flex min-h-0 flex-1 flex-col overflow-hidden">
                  <div className="flex items-center border-b px-2 py-0.5">
                    <SidebarTrigger />
                    <div className="ml-auto">
                      <ConnectionIndicator />
                    </div>
                  </div>
                  <ErrorBoundary section="content">
                    <div className="min-h-0 flex-1 overflow-y-auto">
                      {children}
                    </div>
                  </ErrorBoundary>
                </main>
              </MailDndProvider>
            </SidebarProvider>
          </ThemeProvider>
        </Providers>
      </body>
    </html>
  );
}
