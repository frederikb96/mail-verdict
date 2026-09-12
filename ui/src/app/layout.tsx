import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { Providers } from "@/components/providers";
import { ThemeProvider } from "@/components/theme-provider";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppSidebar } from "@/components/layout/app-sidebar";
import { AppHeader } from "@/components/layout/app-header";
import { SSEConnector } from "@/components/layout/sse-connector";
import { MailDndProvider } from "@/components/mail/dnd-provider";
import { ErrorBoundary } from "@/components/error/error-boundary";
import { OutboxDeadBanner } from "@/components/mail/outbox-dead-banner";
import { UndoSendBanner } from "@/components/mail/undo-send-banner";
import { ToastContainer } from "@/components/common/toast-container";
import { ProtocolHandler } from "@/components/layout/protocol-handler";
import { SectionShortcuts } from "@/components/layout/section-shortcuts";
import { ServiceWorkerNavigation } from "@/components/layout/service-worker-navigation";
import { ShortcutsOverlay } from "@/components/layout/shortcuts-overlay";

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
  // The manifest link is rendered below rather than declared here: it needs
  // `crossorigin="use-credentials"`, which this metadata cannot express.
  appleWebApp: {
    capable: true,
    title: "MailVerdict",
    statusBarStyle: "black-translucent",
  },
  icons: {
    icon: [
      { url: "/icon.svg", type: "image/svg+xml" },
      { url: "/icon-192.png", sizes: "192x192", type: "image/png" },
    ],
    apple: "/apple-touch-icon.png",
  },
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
        {/* Without the credentials hint the manifest is requested anonymously.
            A deployment behind an auth proxy answers that with a login
            redirect, and the browser then installs the site as if it declared
            no manifest at all — named after the page title, with a generated
            letter for an icon. */}
        <link
          rel="manifest"
          href="/manifest.webmanifest"
          crossOrigin="use-credentials"
        />
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var t=localStorage.getItem('theme');var d=t==='dark'||(t!=='light'&&matchMedia('(prefers-color-scheme:dark)').matches);document.documentElement.classList.toggle('dark',d)}catch(e){}})()`
          }}
        />
        <Providers>
          <ThemeProvider>
            <SidebarProvider>
              <SSEConnector />
              <ProtocolHandler />
              <SectionShortcuts />
              <ShortcutsOverlay />
              <ServiceWorkerNavigation />
              <MailDndProvider>
                <ErrorBoundary section="sidebar">
                  <AppSidebar />
                </ErrorBoundary>
                <main className="flex min-h-0 flex-1 flex-col overflow-hidden">
                  <AppHeader />
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
