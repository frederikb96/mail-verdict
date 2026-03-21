"use client";

import { Component, type ErrorInfo, type ReactNode } from "react";
import { ErrorFallback } from "@/components/error/error-fallback";

interface ErrorBoundaryProps {
  children: ReactNode;
  /** Section label for the fallback UI. */
  section?: string;
  /** Optional custom fallback element (overrides default ErrorFallback). */
  fallback?: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * React error boundary that catches render errors in child components
 * and shows a recoverable fallback UI.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(
      `[ErrorBoundary${this.props.section ? `:${this.props.section}` : ""}]`,
      error,
      info.componentStack,
    );
  }

  handleReset = () => {
    this.setState({ error: null });
  };

  render() {
    if (this.state.error) {
      if (this.props.fallback) {
        return this.props.fallback;
      }
      return (
        <ErrorFallback
          section={this.props.section}
          error={this.state.error}
          onReset={this.handleReset}
        />
      );
    }
    return this.props.children;
  }
}
