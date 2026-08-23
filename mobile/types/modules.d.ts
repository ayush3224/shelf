/**
 * React Native ships `Libraries/Network/FormData` without types.
 *
 * It is imported in `__tests__/multipart-body.test.ts` to reproduce the
 * device's FormData rather than the test runner's — the difference between the
 * two is what let a broken multipart body pass its tests.
 */
declare module 'react-native/Libraries/Network/FormData' {
  const FormDataImpl: typeof FormData;
  export default FormDataImpl;
}
