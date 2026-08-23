/** A stand-in for the device info module. Real hardware by default. */
export let isDevice = true;
export const deviceName = 'Test Pixel';
export const manufacturer = 'Google';
export const modelName = 'Pixel 7';

/** Pretend to be an emulator, which has no Play Services and so no token. */
export function __setIsDevice(value: boolean): void {
  isDevice = value;
}
