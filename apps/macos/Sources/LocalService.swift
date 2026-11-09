import Foundation

/// The macOS client owns this adapter. Windows/Linux will supply their own host.
/// Knowledge rules remain behind the versioned kernel API.
typealias LocalService = MacKernelHost
