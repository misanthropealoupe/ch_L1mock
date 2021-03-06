"""
Implementation of L0 processing on for pathfinder pulsar beams.

L0 includes all correlator side processing, including beam forming,
up-channelization, square accumulation, and initial dedispersion. In the
pathfinder mock up, data is recieved as beam formed baseband data. This module
is based off of the packet assembly package: 'ch_vdif_assembler'.

"""

import logging
import time

import numpy as np
import ch_vdif_assembler

from ch_frb_io import stream as io
import constants
import _L0



logger = logging.getLogger(__name__)


# Vdif processors
# ===============

class BaseCorrelator(ch_vdif_assembler.processor):
    """Abstract base class for correlators.

    Subclasses should implement `post_process_intensity` to do something with
    correlated data.

    """

    byte_data = True

    def __init__(self, nframe_integrate=512, **kwargs):
        super(BaseCorrelator, self).__init__(**kwargs)
        self._nframe_integrate = nframe_integrate

    @property
    def delta_t(self):
        return self.nframe_integrate / constants.FPGA_FRAME_RATE

    @property
    def nframe_integrate(self):
        return self._nframe_integrate

    @property
    def freq0(self):
        return constants.FPGA_FREQ0

    @property
    def delta_f(self):
        return constants.FPGA_DELTA_FREQ

    @property
    def nfreq(self):
        return constants.FPGA_NFREQ

    @property
    def freq(self):
        return self.freq0 + np.arange(self.nfreq) * self.delta_f

    @property
    def pol(self):
        return ['XX', 'YY']

    def square_accumulate(self, efield, mask):
        return _L0.square_accumulate(efield, self._nframe_integrate)

    def process_chunk(self, t0, nt, efield, mask):
        ninteg = self._nframe_integrate
        if nt % ninteg:
            # This is currently true of all subclasses.
            msg = ("Number of samples to accumulate (%d) must evenly divide"
                   " number of samples (%d).")
            msg = msg % (ninteg, nt)
            raise ValueError(msg)

        #t0 = time.time()
        intensity, weight = self.square_accumulate(efield, mask)
        #print "Chunk integration time:", time.time() - t0

        time0 = float(t0) / self._nframe_integrate + 1. / 2
        time0 = time0 * self.delta_t

        self.post_process_intensity(time0, intensity, weight)

    def post_process_intensity(self, time0, intensity, weight):
        pass


class ReferenceSqAccumMixin(object):
    """Reference square accumulator, used for testing.

    This mixin can be used to replace the central engine of a correlator with a
    slow, reference, pure-python implementation. This can be usefull for
    testing.

    """

    byte_data = False

    def square_accumulate(self, efield, mask):
        ninteg = self._nframe_integrate

        e_squared = abs(efield)**2
        shape = efield.shape
        new_shape = shape[:-1] + (shape[-1] // ninteg, ninteg)
        e_squared.shape  = new_shape
        mask.shape = new_shape

        # Integrate.
        intensity = np.sum(e_squared, -1, dtype=np.float32)
        weight = np.sum(mask, -1, dtype=np.float32)
        # Normalize for missing data.
        bad_inds = weight == 0
        weight[bad_inds] = 1
        intensity *= ninteg / weight
        # Convert weight to integer between 0 and 255.
        weight *= 255 / ninteg
        weight = np.round(weight).astype(np.uint8)
        weight[bad_inds] = 0

        return intensity, weight


class DummyDataMixin(object):
    """Creates predicatable dummy output data for testing.

    Mixin is used to replace methods in other classes using multiple
    inheritance.

    Data is just ``freq * pol * time``, where ``pol = [1,2]``. In addtion, any data
    with ``0 < (time * 100) % 20 < 1`` is masked (zero weight), as is
    ``0 < (freq/1e6) % 40 < 1``.

    """

    byte_data = True

    def process_chunk(self, t0, nt, efield, mask):
        ninteg = self._nframe_integrate
        out_ntime = efield.shape[-1] // ninteg

        time0 = float(t0) / self._nframe_integrate + 1. / 2
        time0 = time0 * self.delta_t


        # output is data = time * pol * freq
        pol = np.arange(1, 3, dtype=np.float32)
        time = time0 + np.arange(out_ntime) * self.delta_t
        intensity = (self.freq.astype(np.float32)[:,None,None]
                     * pol[:,None]
                     * time.astype(np.float32)
                     )
        weight = np.empty(intensity.shape, dtype=np.uint8)
        weight[:] = 255

        mask_inds_time = (time * 100) % 20 < 1
        intensity[:,:,mask_inds_time] = 0
        weight[:,:,mask_inds_time] = 0

        mask_inds_freq = (self.freq / 1e6) % 40 < 1
        intensity[mask_inds_freq,:,:] = 0
        weight[mask_inds_freq,:,:] = 0

        self.post_process_intensity(time0, intensity, weight)



class CallBackCorrelator(BaseCorrelator):
    """Correlator to which post processing can be added dynamically.

    """

    def __init__(self, *args, **kwargs):
        super(CallBackCorrelator, self).__init__(*args, **kwargs)
        self._callbacks = []
        self._finalizes = []

    def add_callback(self, callback, finalize=None):
        """Add post processing to the correlator.

        The argument `callback` must be a function with the call signature
        `callback(t0, intensity, weight)`.

        """

        self._callbacks.append(callback)
        if finalize is not None:
            self._finalizes.append(finalize)

    def add_diskwrite_callback(self, stream_writer):
        self._stream_writer = stream_writer
        def wrap_absorb(time0, intensity, weight):
            time = time0 + np.arange(intensity.shape[2]) * self.delta_t
            self._stream_writer.absorb_chunk(
                    time=time,
                    intensity=intensity,
                    weight=weight,
                    )
        self.add_callback(wrap_absorb, stream_writer.finalize)

    def post_process_intensity(self, time0, intensity, weight):
        for c in self._callbacks:
            c(time0, intensity, weight)

    def finalize(self):
        for c in self._finalizes:
            c()


class DiskWriteCorrelator(CallBackCorrelator):
    """Correlator that streams output to disk.

    """

    def __init__(self, *args, **kwargs):
        outdir = kwargs.pop('outdir', '')
        super(DiskWriteCorrelator, self).__init__(*args, **kwargs)
        stream_writer = io.StreamWriter(outdir, freq=self.freq, pol=self.pol)
        self.add_diskwrite_callback(stream_writer)





