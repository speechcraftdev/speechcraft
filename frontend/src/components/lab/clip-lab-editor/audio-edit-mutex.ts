/** Single-flight lock for destructive Clip Lab audio mutations. */
export class AudioEditMutex {
  private inFlight = false;

  get isInFlight(): boolean {
    return this.inFlight;
  }

  tryBegin(): boolean {
    if (this.inFlight) return false;
    this.inFlight = true;
    return true;
  }

  end(): void {
    this.inFlight = false;
  }
}
