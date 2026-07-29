

__author__ = 'Konstantin Dmitriev'

from renderchan.module import RenderChanModule
from renderchan.utils import which
from renderchan import ui
import subprocess
import os
import random

class RenderChanFlacModule(RenderChanModule):
    def __init__(self):
        RenderChanModule.__init__(self)
        self.conf['binary']=self.findBinary("flac")
        self.conf['sox_binary']=self.findBinary("sox")
        self.conf["packetSize"]=0

    def getInputFormats(self):
        return ["flac"]

    def getOutputFormats(self):
        return ["wav"]

    def checkRequirements(self):
        if which(self.conf['binary']) == None:
            self.active=False
            ui.info("Module warning (%s): Cannot find '%s' executable." % (self.getName(), self.conf['binary']))
            ui.info("    Please install flac package.")
            return False
        if which(self.conf['sox_binary']) == None:
            self.active=False
            ui.info("Module warning (%s): Cannot find '%s' executable!" % (self.getName(), self.conf['sox_binary']))
            ui.info("    Please install sox package.")
            return False
        self.active=True
        return True

    def render(self, filename, outputPath, startFrame, endFrame, format, updateCompletion, extraParams={}):

        updateCompletion(0.0)

        random_string = "%08d" % (random.randint(0,99999999))
        tmpfile=outputPath+"."+random_string

        # TODO: Progress callback

        commandline=[self.conf['binary'], "-d", filename, "-o", tmpfile]
        subprocess.check_call(commandline, **ui.quiet_subprocess())

        commandline=[self.conf['sox_binary'], tmpfile, outputPath, "rate", "-v", extraParams["audio_rate"]]
        subprocess.check_call(commandline, **ui.quiet_subprocess())

        os.remove(tmpfile)

        updateCompletion(1.0)
