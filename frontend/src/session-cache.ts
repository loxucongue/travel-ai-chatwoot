import { QueryClient } from '@tanstack/react-query';

export function createSessionQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: 5000 } } });
}

export function replaceSessionQueryClient(previous: QueryClient) {
  void previous.cancelQueries();
  previous.clear();
  // A distinct client also isolates late mutations/callbacks holding the old
  // client. Reusing a cleared client would let them populate the next session.
  return createSessionQueryClient();
}
