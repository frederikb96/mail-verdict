import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { Providers } from "@/components/providers";
import { ThemeProvider } from "@/components/theme-provider";
import { SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";
import { AppSidebar } from "@/components/layout/app-sidebar";
import { SSEConnector } from "@/components/layout/sse-connector";
import { MailDndProvider } from "@/components/mail/dnd-provider";
import { ConnectionIndicator } from "@/components/layout/connection-indicator";
import { ErrorBoundary } from "@/components/error/error-boundary";
import { OutboxDeadBanner } from "@/components/mail/outbox-dead-banner";
import { UndoSendBanner } from "@/components/mail/undo-send-banner";
import { ToastContainer } from "@/components/common/toast-container";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

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
    <html
      lang="en"
      className={`h-full antialiased ${geistSans.variable} ${geistMono.variable}`}
      suppressHydrationWarning
    >
      <body className="flex h-full flex-col">
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var t=localStorage.getItem('theme');var d=t==='dark'||(t!=='light'&&matchMedia('(prefers-color-scheme:dark)').matches);document.documentElement.classList.toggle('dark',d)}catch(e){}})()`
          }}
        />
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
                  <OutboxDeadBanner />
                  <UndoSendBanner />
                  <ErrorBoundary section="content">
                    <div className="min-h-0 flex-1 overflow-y-auto">
                      {children}
                    </div>
                  </ErrorBoundary>
                </main>
              </MailDndProvider>
            </SidebarProvider>
          </ThemeProvider>
          <ToastContainer />
        </Providers>
      </body>
    </html>
  );
}
