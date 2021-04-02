"""

"""
import sys
sys.path.insert(0,"../")
from lumo import __version__
print(__version__)


from lumo import Saver

saver = Saver("./sav",max_to_keep=3)
for i in range(10):
    saver.save_keypoint(i,{"a":i},{"b":i})
for i in range(10):
    saver.save_checkpoint(i,{"a":i},{"b":i})
for i in range(10):
    saver.save_model(i,{"a":i},{"b":i})

print(saver.find_keypoints())
print(saver.find_checkpoints())
print(saver.find_models())