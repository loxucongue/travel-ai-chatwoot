"""Linux isolated regression: standby cannot schedule until old owner drains."""
import importlib.util,multiprocessing,tempfile,time
from pathlib import Path
spec=importlib.util.spec_from_file_location('bootstrap',Path(__file__).with_name('worker_bootstrap.py'));b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
def contender(path,event):
    with b.acquire_lock(Path(path),event.set):time.sleep(.1)
def main():
    context=multiprocessing.get_context('spawn')
    with tempfile.TemporaryDirectory() as folder:
        event=context.Event();path=Path(folder)/'lock'
        with b.acquire_lock(path,lambda:None):
            child=context.Process(target=contender,args=(str(path),event));child.start()
            assert not event.wait(.5),'standby processed while old owner held lock'
        assert event.wait(3),'standby failed to take over after drain'
        child.join(3);assert child.exitcode==0
        with b.acquire_lock(path,lambda:None):pass
    print('handoff exclusion, takeover and released-lock reuse: passed')
if __name__=='__main__':main()
