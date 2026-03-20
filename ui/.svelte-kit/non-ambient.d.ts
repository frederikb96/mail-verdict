
// this file is generated — do not edit it


declare module "svelte/elements" {
	export interface HTMLAttributes<T> {
		'data-sveltekit-keepfocus'?: true | '' | 'off' | undefined | null;
		'data-sveltekit-noscroll'?: true | '' | 'off' | undefined | null;
		'data-sveltekit-preload-code'?:
			| true
			| ''
			| 'eager'
			| 'viewport'
			| 'hover'
			| 'tap'
			| 'off'
			| undefined
			| null;
		'data-sveltekit-preload-data'?: true | '' | 'hover' | 'tap' | 'off' | undefined | null;
		'data-sveltekit-reload'?: true | '' | 'off' | undefined | null;
		'data-sveltekit-replacestate'?: true | '' | 'off' | undefined | null;
	}
}

export {};


declare module "$app/types" {
	export interface AppTypes {
		RouteId(): "/" | "/accounts" | "/mail" | "/mail/[id]" | "/search" | "/settings" | "/verdicts";
		RouteParams(): {
			"/mail/[id]": { id: string }
		};
		LayoutParams(): {
			"/": { id?: string };
			"/accounts": Record<string, never>;
			"/mail": { id?: string };
			"/mail/[id]": { id: string };
			"/search": Record<string, never>;
			"/settings": Record<string, never>;
			"/verdicts": Record<string, never>
		};
		Pathname(): "/" | "/accounts" | "/mail" | `/mail/${string}` & {} | "/search" | "/settings" | "/verdicts";
		ResolvedPathname(): `${"" | `/${string}`}${ReturnType<AppTypes['Pathname']>}`;
		Asset(): string & {};
	}
}