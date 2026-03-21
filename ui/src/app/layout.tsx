import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "@/components/providers";
import { ThemeProvider } from "@/components/theme-provider";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppSidebar } from "@/components/layout/app-sidebar";
import { SSEConnector } from "@/components/layout/sse-connector";
import { MailDndProvider } from "@/components/mail/dnd-provider";

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
                <AppSidebar />
                <main className="flex min-h-0 flex-1 flex-col overflow-hidden">
                  {children}
                </main>
              </MailDndProvider>
            </SidebarProvider>
          </ThemeProvider>
        </Providers>
      </body>
    </html>
  );
}
