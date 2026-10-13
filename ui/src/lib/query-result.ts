/**
 * A query result with some of its fields replaced or added, without
 * reading the others.
 *
 * useQuery/useInfiniteQuery hand back a result that records which fields
 * the component reads, and re-render it only when one of those changes. A
 * hook that spreads that result to replace `data` (`{ ...query, data }`)
 * reads every field, `isFetching` and `fetchStatus` included -- so every
 * component using the hook re-renders twice per refetch of its query, for
 * nothing it shows. Fields read through this view are tracked one by one,
 * as if read from the result itself.
 */
export function withFields<R extends object, F extends object>(
  result: R,
  fields: F,
): Omit<R, keyof F> & F {
  const own = (key: string | symbol) => Object.prototype.hasOwnProperty.call(fields, key);
  return new Proxy(result, {
    get(target, key, receiver) {
      return own(key) ? fields[key as keyof F] : Reflect.get(target, key, receiver);
    },
    has(target, key) {
      return own(key) || Reflect.has(target, key);
    },
    ownKeys(target) {
      return [...new Set([...Reflect.ownKeys(target), ...Reflect.ownKeys(fields)])];
    },
    getOwnPropertyDescriptor(target, key) {
      if (!own(key)) return Reflect.getOwnPropertyDescriptor(target, key);
      return { value: fields[key as keyof F], writable: false, enumerable: true, configurable: true };
    },
  }) as unknown as Omit<R, keyof F> & F;
}
