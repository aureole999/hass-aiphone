"""Load the media modules without booting Home Assistant or installing its shim."""

import sys
import types
from pathlib import Path

package = types.ModuleType("aiphone_test")
package.__path__ = [str(Path(__file__).parents[1] / "custom_components" / "aiphone")]
sys.modules[package.__name__] = package
